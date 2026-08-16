#!/usr/bin/env bash
# OpenNVR interactive installer for Linux and macOS.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BASE_COMPOSE="docker-compose.yml"
# MODE controls how already-set values behave:
#   install     — fresh setup; fill missing values, keep existing ones.
#   reconfigure — editing an existing install; re-prompt values with the
#                 current value as the default (Enter keeps, typing changes).
MODE="${1:-install}"
cd "$PROJECT_ROOT"

if [[ ! -t 0 ]]; then
    echo "This installer is interactive. Run it from a terminal: ./scripts/install.sh" >&2
    exit 1
fi

info() { printf '  %s\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }
die() { printf '  ✗ %s\n' "$*" >&2; exit 1; }
# NOTE on `|| true` after every interactive `read`: this script runs under
# `set -e`. A `read` returns non-zero when it hits EOF (stdin closed, or a
# non-interactive/piped invocation that ran out of answers). Without the
# `|| true`, that non-zero status trips errexit and the installer exits
# SILENTLY mid-prompt — e.g. it bailed right after the camera-agent
# voice/chat question. Tolerating the failure lets us fall back to the
# default instead, matching the PowerShell installer (Read-Host has no
# errexit equivalent, which is why start.ps1 never had this bug).
ask_yes_no() {
    local prompt="$1" default="${2:-n}" answer hint
    [[ "$default" == "y" ]] && hint="Y/n" || hint="y/N"
    read -r -p "  $prompt [$hint]: " answer || true
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy] ]]
}
ask_value() {
    local prompt="$1" default="$2" answer
    read -r -p "  $prompt [$default]: " answer || true
    REPLY="${answer:-$default}"
}
ask_secret() {
    local prompt="$1" answer
    read -r -s -p "  $prompt: " answer || true
    printf '\n'
    REPLY="$answer"
}
# Print a short "what this is" block before a prompt.
#   explain <what-it-is> <required?> <default> [where-to-get-it]
explain() {
    printf '  %s\n' "$1"
    printf '    required: %-4s  default: %s\n' "$2" "$3"
    [[ -n "${4:-}" ]] && printf '    note: %s\n' "$4"
}
# Curated, ALWAYS-prompted value with an explanation. Enter keeps the current
# .env value (or the given default on a fresh install); typing overrides it.
configure_value() {
    local key="$1" label="$2" default="$3" what="$4" required="$5" where="${6:-}" current
    current=$(env_get "$key")
    [[ -n "$current" ]] && default="$current"
    printf '\n'
    explain "$what" "$required" "$default" "$where"
    ask_value "$label" "$default"
    env_set "$key" "$REPLY"
}

banner() {
    cat <<'LOGO'

   ___                   _   ___     ______
  / _ \ _ __   ___ _ __ | \ | \ \   / /  _ \
 | | | | '_ \ / _ \ '_ \|  \| |\ \ / /| |_) |
 | |_| | |_) |  __/ | | | |\  | \ V / |  _ <
  \___/| .__/ \___|_| |_|_| \_|  \_/  |_| \_\
       |_|
  Self-hosted NVR — the cameras are yours.

LOGO
}

detect_platform() {
    case "$(uname -s)" in
        Linux*) PLATFORM="Linux"; DEFAULT_RECORDINGS="/var/lib/opennvr/recordings" ;;
        Darwin*) PLATFORM="macOS"; DEFAULT_RECORDINGS="/Users/Shared/opennvr-recordings" ;;
        *) die "Unsupported platform. On Windows run .\\scripts\\install.ps1" ;;
    esac
    ok "Detected $PLATFORM (Docker bridge mode)"
}

# Best-effort IANA timezone of this host, used as the default for the TZ
# prompt. Containers never inherit the host's zone on their own, so whatever
# the operator confirms here must be written to .env explicitly.
detect_timezone() {
    local tz=""
    if command -v timedatectl >/dev/null 2>&1; then
        tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    fi
    if [[ -z "$tz" && -f /etc/timezone ]]; then
        tz=$(cat /etc/timezone 2>/dev/null || true)
    fi
    if [[ -z "$tz" && -L /etc/localtime ]]; then
        tz=$(readlink /etc/localtime 2>/dev/null || true)
        tz="${tz##*zoneinfo/}"
    fi
    printf '%s' "${tz:-UTC}"
}

check_prerequisites() {
    command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker, then re-run."
    docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required. Update Docker and re-run."
    docker info >/dev/null 2>&1 || die "Docker is not running. Start the Docker daemon, then re-run."
    command -v openssl >/dev/null 2>&1 || die "openssl is required to generate credentials"
    [[ -f "$BASE_COMPOSE" ]] || die "$BASE_COMPOSE was not found in $PROJECT_ROOT"
}

env_get() {
    local key="$1" value
    value=$(grep -E "^${key}=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true)
    value=$(printf '%s' "$value" | sed -E 's/[[:space:]]+#.*$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/')
    printf '%s' "$value"
}
env_set() {
    local key="$1" value="$2" tmp found=false line
    tmp=$(mktemp "${PROJECT_ROOT}/.env.tmp.XXXXXX")
    if [[ -f .env ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" == "$key="* ]]; then
                if [[ "$found" == false ]]; then printf '%s=%s\n' "$key" "$value" >> "$tmp"; found=true; fi
            else
                printf '%s\n' "$line" >> "$tmp"
            fi
        done < .env
    fi
    [[ "$found" == true ]] || printf '\n%s=%s\n' "$key" "$value" >> "$tmp"
    mv "$tmp" .env
}
is_missing_or_placeholder() {
    local value="$1"
    [[ -z "$value" || "$value" =~ ^(dev_|insecure_|change_me|your_|changeme|placeholder|dummy|CKLghtP4rWz8J9vN2xQ5mT7yU8kF6bD3eH1aG4cS0wE=) ]]
}
random_hex() { openssl rand -hex "$1"; }
random_password() { openssl rand -hex 16; }
random_fernet() { openssl rand -base64 32 | tr '/+' '_-' | tr -d '\n'; }

ensure_plain_value() {
    local key="$1" label="$2" default="$3" current
    current=$(env_get "$key")
    if [[ -n "$current" ]]; then
        # Fresh install: keep whatever's already there, don't nag.
        [[ "$MODE" == "reconfigure" ]] || return 0
        # Reconfigure: offer the current value as the default so the operator
        # can change it, but Enter keeps it.
        default="$current"
    fi
    ask_value "$label" "$default"
    env_set "$key" "$REPLY"
}
ensure_secret_value() {
    local key="$1" label="$2" generated="$3" current
    current=$(env_get "$key")
    if ! is_missing_or_placeholder "$current"; then
        ok "$label already configured"
        return 0
    fi
    if ask_yes_no "$label is missing or insecure. Use a newly generated value?" y; then
        env_set "$key" "$generated"
    else
        ask_secret "Enter $label"
        [[ -n "$REPLY" ]] || die "$label cannot be empty"
        env_set "$key" "$REPLY"
    fi
    ok "$label configured"
}

prepare_environment() {
    if [[ ! -f .env ]]; then
        [[ -f .env.example ]] || die ".env.example is missing"
        cp .env.example .env
        ok "Created .env from .env.example"
    else
        ok "Using existing .env; existing values will be preserved"
    fi

    # Secrets — generated automatically. Never prompted unless the value is
    # still a placeholder from .env.example (or empty).
    ensure_secret_value POSTGRES_PASSWORD "PostgreSQL password" "$(random_password)"
    ensure_secret_value SECRET_KEY "JWT signing key" "$(random_hex 32)"
    ensure_secret_value CREDENTIAL_ENCRYPTION_KEY "credential encryption key" "$(random_fernet)"
    ensure_secret_value INTERNAL_API_KEY "internal API key" "$(random_password)"
    ensure_secret_value MEDIAMTX_SECRET "MediaMTX webhook secret" "$(random_hex 32)"

    # Rarely-changed database identifiers — filled only if missing (no nagging
    # on a fresh install, editable in reconfigure mode).
    ensure_plain_value POSTGRES_USER "PostgreSQL user" "opennvr_user"
    ensure_plain_value POSTGRES_DB "PostgreSQL database" "opennvr_db"

    # Curated settings most people set. Press Enter to accept the default shown
    # in [brackets]; type a value to change it. All are local — no accounts,
    # no API keys.
    printf '\n  ── Basic settings ─────────────────────────────────────\n'
    configure_value DEFAULT_ADMIN_USERNAME "Administrator username" "admin" \
        "Login name for the first OpenNVR admin account." "yes" \
        "You pick this yourself — no external account involved."
    configure_value DEFAULT_ADMIN_EMAIL "Administrator email" "admin@opennvr.local" \
        "Contact email tied to the admin account." "yes" \
        "Any address works; the placeholder is fine for an offline setup."
    configure_value RECORDINGS_PATH "Recordings folder on this machine" "$DEFAULT_RECORDINGS" \
        "Host directory where recorded video segments are written." "yes" \
        "Created automatically if it does not exist yet."
    configure_value TZ "Timezone (IANA name)" "$(detect_timezone)" \
        "Timezone used to name recording folders and align the playback timeline." "yes" \
        "Auto-detected from this machine; the recorder and backend containers both use it."
    local tz_value
    tz_value=$(env_get TZ)
    [[ "$tz_value" == "UTC" || "$tz_value" == */* ]] || \
        warn "'$tz_value' does not look like an IANA timezone (e.g. Asia/Kolkata); the containers will fall back to UTC if it is invalid"

    mkdir -p "$(env_get RECORDINGS_PATH)" 2>/dev/null || warn "Could not create the recordings directory; Docker will try"
}

find_example_compose() {
    local name="$1" candidate
    for candidate in "docker-compose.${name}.yml" "docker-compose.${name}.yaml" \
                     "examples/${name}/docker-compose.yml" "examples/${name}/docker-compose.yaml" \
                     "examples/${name}/compose.yml" "examples/${name}/compose.yaml"; do
        [[ -f "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
    done
    return 1
}

prompt_overlay_defaults() {
    # Collect the specs into an array FIRST. Feeding them to the loop via
    # `done < <(grep ...)` redirected the loop's stdin — and ask_value's
    # plain `read` then consumed the NEXT SPEC as the "answer" instead of
    # asking the terminal. Net effect: no prompts shown, and .env entries
    # like LLAMACPP_GPU_LAYERS='${NGINX_BIND_HOST:-0.0.0.0}' which compose
    # interpolates to 0.0.0.0 → the llamacpp adapter crashed on int().
    local file="$1" spec body key default current specs=()
    mapfile -t specs < <(grep -oE '\$\{[A-Z][A-Z0-9_]*:-[^}]+\}' "$file" | sort -u || true)
    for spec in "${specs[@]}"; do
        [[ -n "$spec" ]] || continue
        body="${spec#\$\{}"; body="${body%\}}"
        key="${body%%:-*}"; default="${body#*:-}"
        current=$(env_get "$key")
        [[ -n "$current" ]] && continue
        ask_value "$key" "$default"
        env_set "$key" "$REPLY"
    done
}

choose_example() {
    EXAMPLE_NAME=""; EXAMPLE_COMPOSE=""; EXAMPLE_PROFILE=""
    env_set OPENNVR_EXAMPLE ""
    env_set OPENNVR_EXAMPLE_COMPOSE ""
    env_set OPENNVR_EXAMPLE_PROFILE ""

    printf '\n  ── Example app ────────────────────────────────────────\n'
    info "Examples add an AI app on top of the core NVR. The Camera Agent lets you"
    info "ask your cameras questions out loud or by chat. Everything runs locally."
    ask_yes_no "Set up an example app now?" n || return 0
    local names=() dir name manifest choice index
    while IFS= read -r dir; do names+=("$(basename "$dir")"); done < <(find examples -mindepth 1 -maxdepth 1 -type d | sort)
    [[ ${#names[@]} -gt 0 ]] || { warn "No examples were found"; return 0; }

    printf '\n  Available examples:\n'
    index=1
    for name in "${names[@]}"; do
        if manifest=$(find_example_compose "$name"); then
            printf '  %2d. %-30s [installable: %s]\n' "$index" "$name" "$manifest"
        else
            printf '  %2d. %-30s [no Compose manifest]\n' "$index" "$name"
        fi
        index=$((index + 1))
    done
    printf '   0. Core stack only\n\n'
    read -r -p "  Select an example [0]: " choice || true  # EOF-safe under set -e (see ask_* note)
    choice="${choice:-0}"
    [[ "$choice" =~ ^[0-9]+$ ]] || die "Invalid selection"
    (( choice == 0 )) && return 0
    (( choice >= 1 && choice <= ${#names[@]} )) || die "Selection out of range"

    name="${names[$((choice - 1))]}"
    manifest=$(find_example_compose "$name") || die "The '$name' example has no Docker Compose manifest"
    EXAMPLE_NAME="$name"; EXAMPLE_COMPOSE="$manifest"; EXAMPLE_PROFILE="$name"
    if [[ "$name" == "camera-agent" ]]; then
        printf '\n'
        explain "Camera Agent runs in VOICE mode (speak, hear spoken answers) or CHAT mode (type, read answers). Voice adds Whisper speech-to-text and Piper text-to-speech; chat is lighter." \
            "pick one" "1 (voice)"
        ask_value "Camera Agent mode: 1=voice, 2=chat" "1"
        [[ "$REPLY" == "2" ]] && EXAMPLE_PROFILE="camera-agent-chat" || EXAMPLE_PROFILE="camera-agent"

        printf '\n  ── Camera Agent models (all local, no API keys) ───────\n'
        configure_value OLLAMA_MODEL "Local LLM model (Ollama)" "qwen2.5:1.5b" \
            "The local chat model that answers your questions; must support tool calling." "yes" \
            "Pulled automatically. qwen2.5:0.5b (low RAM) | 1.5b (default) | 3b (better, slower)."
        if [[ "$EXAMPLE_PROFILE" == "camera-agent" ]]; then
            configure_value WHISPER_MODEL_SIZE "Whisper speech-to-text model" "base.en" \
                "Transcribes your spoken questions (voice mode only)." "yes" \
                "tiny.en (fastest) | base.en (default) | small.en (most accurate)."
        fi
        configure_value CAPTION_ADAPTER "Scene-description model" "moondream" \
            "Describes what a camera sees. moondream answers questions (VQA); blip writes plain captions." "yes" \
            "moondream | blip — both run locally."
    else
        # Generic examples: prompt for any ${VAR:-default} the overlay exposes.
        prompt_overlay_defaults "$manifest"
    fi
    env_set OPENNVR_EXAMPLE "$EXAMPLE_NAME"
    env_set OPENNVR_EXAMPLE_COMPOSE "$EXAMPLE_COMPOSE"
    env_set OPENNVR_EXAMPLE_PROFILE "$EXAMPLE_PROFILE"
    ok "Selected $EXAMPLE_NAME ($EXAMPLE_PROFILE)"
    if [[ "$name" == "camera-agent" ]]; then
        info "The local LLM model downloads on first start — usually the slowest step."
    fi
}

pull_and_build() {
    printf '\n'
    info "First-time setup downloads several container images (and, for the"
    info "Camera Agent, a local LLM model of ~1 GB). Depending on your network"
    info "this can take 8-15 minutes. Later starts are much faster — everything"
    info "is cached, so you only pay this cost once."
    printf '\n'
    info "Pulling the OpenNVR core stack..."
    docker compose -f "$BASE_COMPOSE" pull --ignore-buildable

    choose_example
    COMPOSE_ARGS=(-f "$BASE_COMPOSE")
    if [[ -n "$EXAMPLE_COMPOSE" ]]; then
        COMPOSE_ARGS+=(-f "$EXAMPLE_COMPOSE" --profile "$EXAMPLE_PROFILE")
        info "Pulling images for $EXAMPLE_NAME..."
        docker compose "${COMPOSE_ARGS[@]}" pull --ignore-buildable
    fi
    info "Building services that do not publish a pre-built image..."
    docker compose "${COMPOSE_ARGS[@]}" build
}

main() {
    banner
    printf '  OpenNVR interactive installer\n\n'
    detect_platform
    check_prerequisites
    prepare_environment
    pull_and_build
    printf '\n  Configuration and images are ready. Starting OpenNVR...\n\n'
    exec "$PROJECT_ROOT/start.sh" up
}
main "$@"