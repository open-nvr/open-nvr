#!/usr/bin/env bash
# ============================================================
# OpenNVR - Interactive Installation Wizard (Linux / macOS)
# ============================================================
# Called automatically by start.sh on first run.
# Can also be re-run manually: ./scripts/install.sh
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# ── ANSI colours ──────────────────────────────────────────
BOLD='\033[1m'
ORANGE='\033[38;5;208m'
AMBER='\033[38;5;214m'
BRIGHT_BLUE='\033[1;34m'
DARK_BLUE='\033[0;34m'
CYAN='\033[0;36m'
BRIGHT_CYAN='\033[1;36m'
GREEN='\033[0;32m'
BRIGHT_GREEN='\033[1;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
WHITE='\033[1;37m'
GRAY='\033[38;5;245m'
NC='\033[0m'

# ── Collected settings (populated by wizard) ──────────────
OS_NAME=""
COMPOSE_FILE=""
DEPLOY_MODE="quick"
RECORDINGS_PATH=""
AI_ENABLED=false
AI_REPO_URL="https://github.com/open-nvr/ai-adapter.git"
ADMIN_USERNAME="admin"
ADMIN_EMAIL="admin@opennvr.local"
POSTGRES_PASSWORD=""
SECRET_KEY=""
CREDENTIAL_ENCRYPTION_KEY=""
INTERNAL_API_KEY=""
MEDIAMTX_SECRET=""

# ╔══════════════════════════════════════════════════════════╗
# ║  LOGO                                                    ║
# ╚══════════════════════════════════════════════════════════╝
print_logo() {
    clear
    echo ""
    echo -e "${WHITE}${BOLD}  ██████╗ ██████╗ ███████╗███╗   ██╗${NC}${DARK_BLUE}${BOLD} ███╗   ██╗██╗   ██╗██████╗ ${NC}"
    echo -e "${WHITE}${BOLD} ██╔═══██╗██╔══██╗██╔════╝████╗  ██║${NC}${DARK_BLUE}${BOLD} ████╗  ██║██║   ██║██╔══██╗${NC}"
    echo -e "${WHITE}${BOLD} ██║   ██║██████╔╝█████╗  ██╔██╗ ██║${NC}${DARK_BLUE}${BOLD} ██╔██╗ ██║██║   ██║██████╔╝${NC}"
    echo -e "${WHITE}${BOLD} ██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║${NC}${DARK_BLUE}${BOLD} ██║╚██╗██║╚██╗ ██╔╝██╔══██╗${NC}"
    echo -e "${WHITE}${BOLD} ╚██████╔╝██║     ███████╗██║ ╚████║${NC}${DARK_BLUE}${BOLD} ██║ ╚████║ ╚████╔╝ ██║  ██║${NC}"
    echo -e "${WHITE}${BOLD}  ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝${NC}${DARK_BLUE}${BOLD} ╚═╝  ╚═══╝  ╚═══╝  ╚═╝  ╚═╝${NC}"
    echo ""
    echo -e "  ${WHITE}${BOLD}Open Source Network Video Recorder${NC}"
    echo -e "  ${GRAY}Self-Hosted · AI-Ready · Privacy-First${NC}"
    echo ""
    echo -e "  ${GRAY}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${AMBER}  Installation Wizard${NC}"
    echo -e "  ${GRAY}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

# ╔══════════════════════════════════════════════════════════╗
# ║  UI HELPERS                                              ║
# ╚══════════════════════════════════════════════════════════╝
section() {
    echo ""
    echo -e "  ${BRIGHT_CYAN}${BOLD}◈  $1${NC}"
    echo -e "  ${GRAY}$(printf '─%.0s' {1..50})${NC}"
    echo ""
}

ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "  ${RED}✗${NC}  $1"; }
info() { echo -e "  ${GRAY}·${NC}  $1"; }
step() { echo -e "  ${CYAN}→${NC}  $1"; }

ask() {
    local prompt="$1"
    local default="$2"
    local result
    read -r -p "$(echo -e "  ${YELLOW}?${NC}  ${WHITE}${prompt}${NC} ${GRAY}[${default}]${NC}: ")" result
    echo "${result:-$default}"
}

ask_yn() {
    local prompt="$1"
    local default="${2:-n}"
    local hint result
    [ "$default" = "y" ] && hint="${BRIGHT_GREEN}Y${NC}${GRAY}/n${NC}" || hint="${GRAY}y/${NC}${RED}N${NC}"
    read -r -p "$(echo -e "  ${YELLOW}?${NC}  ${WHITE}${prompt}${NC} ${GRAY}[${hint}${GRAY}]${NC}: ")" result
    result="${result:-$default}"
    [[ "$result" =~ ^[Yy] ]]
}

ask_secret() {
    local prompt="$1"
    local result
    read -r -s -p "$(echo -e "  ${YELLOW}?${NC}  ${WHITE}${prompt}${NC}: ")" result
    echo ""
    echo "$result"
}

ask_choice() {
    local prompt="$1"
    shift
    local default="$1"
    shift
    local choice
    read -r -p "$(echo -e "  ${YELLOW}?${NC}  ${WHITE}${prompt}${NC} ${GRAY}[${default}]${NC}: ")" choice
    echo "${choice:-$default}"
}

spinner_pid=""
start_spinner() {
    local msg="$1"
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    local i=0
    while true; do
        printf "\r  ${BRIGHT_CYAN}${frames[$((i % 10))]}${NC}  $msg"
        i=$((i + 1))
        sleep 0.08
    done &
    spinner_pid=$!
    disown "$spinner_pid" 2>/dev/null || true
}

stop_spinner() {
    if [[ -n "${spinner_pid:-}" ]]; then
        kill "$spinner_pid" 2>/dev/null || true
        wait "$spinner_pid" 2>/dev/null || true
        spinner_pid=""
        printf "\r\033[K"
    fi
}

# ╔══════════════════════════════════════════════════════════╗
# ║  STEP 1 — PREREQUISITES                                  ║
# ╚══════════════════════════════════════════════════════════╝
detect_os() {
    OS_TYPE="$(uname -s)"
    case "$OS_TYPE" in
        Linux*)
            OS_NAME="Linux"
            COMPOSE_FILE="docker-compose.linux.yml"
            ;;
        Darwin*)
            OS_NAME="macOS"
            COMPOSE_FILE="docker-compose.yml"
            ;;
        *)
            OS_NAME="Unknown"
            COMPOSE_FILE="docker-compose.yml"
            warn "Unrecognised OS: $OS_TYPE — defaulting to bridge network mode"
            ;;
    esac
}

check_prereqs() {
    section "Checking prerequisites"
    local errors=0

    info "Detected OS: ${WHITE}${OS_NAME}${NC}"
    info "Compose file: ${WHITE}${COMPOSE_FILE}${NC}"
    echo ""

    if ! command -v docker &>/dev/null; then
        fail "Docker is not installed"
        step "Install: https://docs.docker.com/get-docker/"
        errors=$((errors + 1))
    else
        ok "Docker  $(docker --version 2>/dev/null | sed 's/Docker version /v/' | cut -d',' -f1)"
    fi

    if command -v docker &>/dev/null && ! docker info &>/dev/null 2>&1; then
        fail "Docker daemon is not running"
        step "Start Docker Desktop or: sudo systemctl start docker"
        errors=$((errors + 1))
    elif command -v docker &>/dev/null; then
        ok "Docker daemon is running"
    fi

    if ! docker compose version &>/dev/null 2>&1; then
        fail "Docker Compose v2 not found (required)"
        step "Update Docker Desktop or: https://docs.docker.com/compose/install/"
        errors=$((errors + 1))
    else
        ok "Docker Compose  $(docker compose version 2>/dev/null | sed 's/Docker Compose version /v/')"
    fi

    if ! command -v git &>/dev/null; then
        warn "Git is not installed — AI adapter cloning will not be available"
    else
        ok "Git  $(git --version 2>/dev/null)"
    fi

    if [[ $errors -gt 0 ]]; then
        echo ""
        fail "Please fix the errors above and re-run the installer."
        exit 1
    fi
}

# ╔══════════════════════════════════════════════════════════╗
# ║  STEP 2 — DEPLOYMENT MODE                               ║
# ╚══════════════════════════════════════════════════════════╝
ask_deploy_mode() {
    section "Deployment mode"
    echo -e "  ${WHITE}How do you want to set up OpenNVR?${NC}"
    echo ""
    echo -e "   ${BRIGHT_CYAN}1${NC}  ${WHITE}Quick start${NC}   ${GRAY}· Sensible dev defaults · Up in seconds${NC}"
    echo -e "   ${BRIGHT_CYAN}2${NC}  ${WHITE}Production${NC}    ${GRAY}· Strong auto-generated secrets · Recommended${NC}"
    echo ""
    local choice
    choice=$(ask_choice "Choose" "1")
    case "$choice" in
        2) DEPLOY_MODE="production"; ok "Production mode — all secrets will be uniquely generated" ;;
        *) DEPLOY_MODE="quick";      ok "Quick start mode — you can harden later with: ./scripts/generate-secrets.sh --write" ;;
    esac
}

# ╔══════════════════════════════════════════════════════════╗
# ║  STEP 3 — RECORDING STORAGE                             ║
# ╚══════════════════════════════════════════════════════════╝
ask_recordings_path() {
    section "Recording storage"
    echo -e "  ${WHITE}Where should camera recordings be stored on this machine?${NC}"
    echo -e "  ${GRAY}This path is bind-mounted into the Docker containers.${NC}"
    echo ""

    local default_path
    case "$OS_NAME" in
        Linux)  default_path="/var/lib/opennvr/recordings" ;;
        macOS)  default_path="/Users/Shared/opennvr-recordings" ;;
        *)      default_path="./recordings" ;;
    esac

    RECORDINGS_PATH=$(ask "Recordings path" "$default_path")

    if [[ ! -d "$RECORDINGS_PATH" ]]; then
        if mkdir -p "$RECORDINGS_PATH" 2>/dev/null; then
            ok "Created: $RECORDINGS_PATH"
        else
            warn "Could not create directory — Docker will attempt to create it on start"
        fi
    else
        ok "Directory exists: $RECORDINGS_PATH"
    fi
}

# ╔══════════════════════════════════════════════════════════╗
# ║  STEP 4 — AI DETECTION                                  ║
# ╚══════════════════════════════════════════════════════════╝
ask_ai() {
    section "AI-powered detection (optional)"
    echo -e "  ${WHITE}OpenNVR supports AI object detection via a separate adapter service.${NC}"
    echo -e "  ${GRAY}Requires cloning an extra repository (done automatically).${NC}"
    echo ""

    if ask_yn "Enable AI detection?" "n"; then
        AI_ENABLED=true
        echo ""
        AI_REPO_URL=$(ask "AI adapter repository URL" "https://github.com/open-nvr/ai-adapter.git")
        ok "AI detection will be enabled"
    else
        AI_ENABLED=false
        ok "AI detection disabled — enable later by re-running: ./scripts/install.sh"
    fi
}

# ╔══════════════════════════════════════════════════════════╗
# ║  STEP 5 — ADMIN ACCOUNT                                 ║
# ╚══════════════════════════════════════════════════════════╝
ask_admin() {
    section "Administrator account"
    echo -e "  ${WHITE}Set a username and email for the default admin account.${NC}"
    echo -e "  ${GRAY}You will complete the full account setup (including password)${NC}"
    echo -e "  ${GRAY}at the first-time setup page after installation.${NC}"
    echo ""

    ADMIN_USERNAME=$(ask "Username" "admin")
    ADMIN_EMAIL=$(ask "Email"    "admin@opennvr.local")
}

# ╔══════════════════════════════════════════════════════════╗
# ║  STEP 6 — SECRETS                                       ║
# ╚══════════════════════════════════════════════════════════╝
generate_secrets() {
    section "Generating secrets"

    POSTGRES_PASSWORD=$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)
    SECRET_KEY=$(openssl rand -hex 32)
    INTERNAL_API_KEY=$(openssl rand -base64 32 | tr -d '\n=')
    MEDIAMTX_SECRET=$(openssl rand -hex 32)

    CREDENTIAL_ENCRYPTION_KEY=""
    for pybin in python3 python; do
        if command -v "$pybin" &>/dev/null; then
            CREDENTIAL_ENCRYPTION_KEY=$("$pybin" -c \
                "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
                2>/dev/null || true)
            [[ -n "$CREDENTIAL_ENCRYPTION_KEY" ]] && break
        fi
    done

    if [[ -z "$CREDENTIAL_ENCRYPTION_KEY" ]]; then
        warn "Python cryptography library not found — using openssl fallback"
        info "For proper Fernet keys: pip install cryptography"
        CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr -d '\n')
    fi

    ok "Database password generated"
    ok "JWT secret key generated"
    ok "Credential encryption key generated"
    ok "Internal API key generated"
    ok "MediaMTX webhook secret generated"
}

# ╔══════════════════════════════════════════════════════════╗
# ║  STEP 7 — CLONE AI ADAPTER                              ║
# ╚══════════════════════════════════════════════════════════╝
clone_ai_adapter() {
    [[ "$AI_ENABLED" != true ]] && return 0

    section "Cloning AI adapter"
    local target_dir
    target_dir="$(cd "$PROJECT_ROOT/.." && pwd)/ai-adapter"

    if [[ -d "$target_dir" ]]; then
        warn "Directory already exists: $target_dir"
        if ask_yn "Skip cloning and use existing directory?" "y"; then
            ok "Using existing ai-adapter at: $target_dir"
            return 0
        fi
        step "Removing existing directory..."
        rm -rf "$target_dir"
    fi

    if ! command -v git &>/dev/null; then
        fail "Git is not installed — cannot clone AI adapter"
        warn "Clone manually: git clone $AI_REPO_URL $target_dir"
        AI_ENABLED=false
        return 0
    fi

    start_spinner "Cloning $AI_REPO_URL ..."
    if git clone "$AI_REPO_URL" "$target_dir" 2>/dev/null; then
        stop_spinner
        ok "AI adapter cloned to: $target_dir"

        # Fix model_weights ownership for container user (uid 1000)
        local weights_dir="$target_dir/model_weights"
        mkdir -p "$weights_dir"
        chown -R 1000:1000 "$weights_dir" 2>/dev/null || true
    else
        stop_spinner
        fail "Clone failed — check the URL and your internet connection"
        warn "Continuing without AI detection"
        AI_ENABLED=false
    fi
}

# ╔══════════════════════════════════════════════════════════╗
# ║  STEP 8 — WRITE .env                                    ║
# ╚══════════════════════════════════════════════════════════╝
write_env() {
    section "Writing configuration"

    local adapter_url_line
    if [[ "$AI_ENABLED" == true ]]; then
        case "$OS_NAME" in
            Linux) adapter_url_line="ADAPTER_URL=http://127.0.0.1:9100" ;;
            *)     adapter_url_line="ADAPTER_URL=http://opennvr_ai:9100" ;;
        esac
    else
        adapter_url_line="# ADAPTER_URL=  # Uncomment when AI adapters are enabled"
    fi

    cat > .env <<EOF
# ============================================================
# OpenNVR Configuration
# Generated by installer on $(date '+%Y-%m-%d %H:%M:%S')
# ============================================================

# ── DATABASE ─────────────────────────────────────────────
POSTGRES_USER=opennvr_user
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=opennvr_db

# ── SECURITY ─────────────────────────────────────────────
SECRET_KEY=${SECRET_KEY}
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=120
# WARNING: Never change CREDENTIAL_ENCRYPTION_KEY after first run
CREDENTIAL_ENCRYPTION_KEY=${CREDENTIAL_ENCRYPTION_KEY}
INTERNAL_API_KEY=${INTERNAL_API_KEY}
MEDIAMTX_SECRET=${MEDIAMTX_SECRET}

# ── APPLICATION ───────────────────────────────────────────
DEBUG=False
HOST=0.0.0.0
PORT=8000
APPLICATION_URL=http://localhost:8000
API_PREFIX=/api/v1

# ── MEDIAMTX ─────────────────────────────────────────────
MEDIAMTX_BASE_URL=http://localhost:8889
MEDIAMTX_ADMIN_API=http://localhost:9997/v3
MEDIAMTX_API_URL=http://localhost:9997
MEDIAMTX_HLS_URL=http://localhost:8888
MEDIAMTX_RTSP_URL=rtsp://localhost:8554
MEDIAMTX_PLAYBACK_URL=http://localhost:9996
MEDIAMTX_STREAM_PREFIX=cam-
MEDIAMTX_PATH_MODE=id
MEDIAMTX_AUTO_PROVISION=True

# ── DOCKER NETWORKING ─────────────────────────────────────
BACKEND_HOST=opennvr_core
BACKEND_PORT=8000

# ── RECORDING STORAGE ─────────────────────────────────────
RECORDINGS_PATH=${RECORDINGS_PATH}

# ── AI INFERENCE ──────────────────────────────────────────
AI_ENABLED=$([ "$AI_ENABLED" = true ] && echo "true" || echo "false")
KAI_C_URL=http://127.0.0.1:8100
KAI_C_IP=127.0.0.1
${adapter_url_line}

# ── ADMIN USER ────────────────────────────────────────────
DEFAULT_ADMIN_USERNAME=${ADMIN_USERNAME}
DEFAULT_ADMIN_EMAIL=${ADMIN_EMAIL}
DEFAULT_ADMIN_FIRST_NAME=System
DEFAULT_ADMIN_LAST_NAME=Administrator

# ── LOGGING ───────────────────────────────────────────────
LOG_LEVEL=INFO
LOG_FILE_ENABLED=True
LOG_FILE_PATH=logs/server.log
LOG_FILE_MAX_SIZE_MB=50
LOG_FILE_BACKUP_COUNT=10
LOG_CONSOLE_ENABLED=True
LOG_JSON_FORMAT=False
EOF

    ok ".env written successfully"
}

# ╔══════════════════════════════════════════════════════════╗
# ║  SUMMARY                                                 ║
# ╚══════════════════════════════════════════════════════════╝
print_summary() {
    echo ""
    echo -e "  ${GRAY}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  ${WHITE}${BOLD}  Installation Summary${NC}"
    echo ""
    echo -e "  ${GRAY}  Platform       ${NC}${WHITE}${OS_NAME}${NC}  ${GRAY}(${COMPOSE_FILE})${NC}"
    echo -e "  ${GRAY}  Mode           ${NC}${WHITE}${DEPLOY_MODE}${NC}"
    echo -e "  ${GRAY}  Recordings     ${NC}${WHITE}${RECORDINGS_PATH}${NC}"

    if [[ "$AI_ENABLED" == true ]]; then
        echo -e "  ${GRAY}  AI Detection   ${NC}${BRIGHT_GREEN}enabled${NC}"
    else
        echo -e "  ${GRAY}  AI Detection   ${NC}${GRAY}disabled${NC}"
    fi

    echo ""
    echo -e "  ${GRAY}  Admin user     ${NC}${WHITE}${ADMIN_USERNAME}${NC}"
    echo -e "  ${GRAY}  Admin email    ${NC}${WHITE}${ADMIN_EMAIL}${NC}"
    echo ""
    echo -e "  ${CYAN}  → Complete password setup at the first-time setup page.${NC}"
    echo ""
    echo -e "  ${GRAY}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# ╔══════════════════════════════════════════════════════════╗
# ║  LAUNCH                                                  ║
# ╚══════════════════════════════════════════════════════════╝
launch_services() {
    echo ""
    if ! ask_yn "Build and start OpenNVR now?" "y"; then
        echo ""
        info "Configuration saved to .env"
        echo ""
        step "To start later:"
        echo -e "  ${BRIGHT_CYAN}  ./start.sh build${NC}   ${GRAY}(first run — builds Docker images)${NC}"
        echo -e "  ${BRIGHT_CYAN}  ./start.sh${NC}          ${GRAY}(subsequent starts)${NC}"
        echo ""
        return 0
    fi

    echo ""
    section "Starting OpenNVR"

    local profile_arg=""
    [[ "$AI_ENABLED" == true ]] && profile_arg="--profile ai"

    start_spinner "Building Docker images (this may take a few minutes on first run) ..."
    if docker compose -f "$COMPOSE_FILE" $profile_arg build 2>&1 | \
        grep -E "^(Step|STEP|#[0-9]|Successfully|ERROR)" || true; then
        :
    fi
    stop_spinner
    ok "Docker images built"

    start_spinner "Starting all services ..."
    docker compose -f "$COMPOSE_FILE" $profile_arg up -d
    stop_spinner
    ok "All services started"

    # Wait briefly for the health check to get going
    sleep 3

    echo ""
    echo -e "  ${GRAY}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  ${BRIGHT_GREEN}${BOLD}  ✓  OpenNVR is running!${NC}"
    echo ""
    echo -e "  ${GRAY}  Web Interface${NC}  →  ${BRIGHT_CYAN}http://localhost:8000${NC}"
    echo -e "  ${GRAY}  API Docs     ${NC}  →  ${BRIGHT_CYAN}http://localhost:8000/docs${NC}"
    echo -e "  ${GRAY}  First-time setup page opens automatically on first visit.${NC}"
    echo ""
    echo -e "  ${GRAY}  Useful commands:${NC}"
    echo -e "  ${GRAY}    ./start.sh logs    ${NC}${WHITE}# follow live logs${NC}"
    echo -e "  ${GRAY}    ./start.sh status  ${NC}${WHITE}# check container health${NC}"
    echo -e "  ${GRAY}    ./start.sh down    ${NC}${WHITE}# stop all services${NC}"
    echo ""
    echo -e "  ${GRAY}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

# ╔══════════════════════════════════════════════════════════╗
# ║  MAIN                                                    ║
# ╚══════════════════════════════════════════════════════════╝
main() {
    print_logo

    # If .env already exists, confirm overwrite
    if [[ -f "$PROJECT_ROOT/.env" ]]; then
        echo -e "  ${YELLOW}⚠  An existing .env was found.${NC}"
        echo ""
        if ! ask_yn "Reconfigure and overwrite existing settings?" "n"; then
            echo ""
            info "Installation cancelled. Your .env is unchanged."
            echo ""
            exit 0
        fi
        echo ""
    fi

    detect_os
    check_prereqs
    ask_deploy_mode
    ask_recordings_path
    ask_ai
    ask_admin
    generate_secrets
    clone_ai_adapter
    write_env
    print_summary
    launch_services
}

main
