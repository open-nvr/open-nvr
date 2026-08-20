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
    # An if, NOT ``[[ -n ... ]] && printf``: with no note argument the
    # && list makes the function's exit status 1, and under ``set -e``
    # the bare ``explain ...`` call kills the installer SILENTLY — the
    # camera-agent selection died exactly here (the only call site
    # without a note). Same failure family as the ``read || true``
    # rule documented above; keep explain() unconditionally returning 0.
    if [[ -n "${4:-}" ]]; then
        printf '    note: %s\n' "$4"
    fi
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
    # HOST_ARCH is Docker's platform vocabulary, not uname's. `docker compose
    # pull` resolves every image's manifest list against linux/$HOST_ARCH, and
    # when an image has no entry for it the daemon aborts the whole pull with a
    # bare "no matching manifest for linux/arm64/v8" — no image name, no
    # explanation, several services in. check_image_architectures() below turns
    # that into a named list before anything is downloaded.
    case "$(uname -m)" in
        x86_64|amd64)  HOST_ARCH="amd64" ;;
        arm64|aarch64) HOST_ARCH="arm64" ;;
        armv7l)        HOST_ARCH="arm" ;;
        # Unknown machine type: leave HOST_ARCH empty and skip the preflight
        # rather than guess a platform string and reject a valid install.
        *)             HOST_ARCH="" ;;
    esac
    ok "Detected $PLATFORM/${HOST_ARCH:-$(uname -m)} (Docker bridge mode)"
}

# Does this image's manifest list carry an entry for the host architecture?
#   0 — yes
#   1 — the image resolves, but has no build for this architecture
#   2 — undeterminable (no buildx, private image, registry hiccup, or a
#       single-arch manifest with no platform metadata). Never a reason to
#       block an install: a false "unsupported" is worse than the raw
#       daemon error we are replacing.
image_supports_host_arch() {
    local image="$1" raw
    raw=$(docker buildx imagetools inspect --raw "$image" 2>/dev/null) || return 2
    # A bare image manifest (schema 2, single platform) has no "manifests"
    # key and therefore no platform metadata to check.
    case "$raw" in
        *'"manifests"'*) ;;
        *) return 2 ;;
    esac
    # Buildx attestation entries appear alongside the real ones with
    # "architecture":"unknown", so matching the host arch specifically is
    # what distinguishes a usable build from provenance metadata.
    if printf '%s' "$raw" | tr -d ' \t\r\n' | grep -q "\"architecture\":\"${HOST_ARCH}\""; then
        return 0
    fi
    return 1
}

# Preflight: name the images that cannot run here, before the pull starts.
# Only meaningful off amd64 — every image in the stack publishes amd64.
check_image_architectures() {
    [[ -n "$HOST_ARCH" && "$HOST_ARCH" != "amd64" ]] || return 0
    docker buildx version >/dev/null 2>&1 || return 0

    # A newline-delimited string plus an explicit counter, NOT an array:
    # macOS still ships bash 3.2, where `${#arr[@]}` on an empty array under
    # `set -u` is an "unbound variable" error — and an empty list is the
    # normal, successful outcome of this function.
    local images image rc unsupported="" count=0
    images=$(docker compose -f "$BASE_COMPOSE" config --images 2>/dev/null) || return 0
    [[ -n "$images" ]] || return 0

    info "Checking that the pinned images publish a linux/${HOST_ARCH} build..."
    while IFS= read -r image; do
        [[ -n "$image" ]] || continue
        rc=0
        image_supports_host_arch "$image" || rc=$?
        if [[ $rc -eq 1 ]]; then
            unsupported+="${image}"$'\n'
            count=$((count + 1))
        fi
    done <<< "$images"

    if [[ $count -eq 0 ]]; then
        ok "Every core image has a linux/${HOST_ARCH} build"
        return 0
    fi

    printf '\n'
    warn "This machine is ${PLATFORM}/${HOST_ARCH}. These images publish no linux/${HOST_ARCH} build:"
    printf '%s' "$unsupported" | while IFS= read -r image; do
        printf '      %s\n' "$image"
    done
    printf '\n'

    if [[ "${OPENNVR_ALLOW_EMULATION:-0}" == "1" ]]; then
        # Exported, not just set: the installer ends with `exec start.sh up`,
        # and every later `docker compose` call has to resolve the same way
        # or the stack comes up half-emulated and half-broken.
        export DOCKER_DEFAULT_PLATFORM=linux/amd64
        warn "OPENNVR_ALLOW_EMULATION=1 — pulling the linux/amd64 builds and running"
        warn "them under emulation. Detection latency will be several times worse."
        warn "On Docker Desktop, enable Settings -> General -> 'Use Rosetta for"
        warn "x86_64/amd64 emulation' first, or containers may fail to start at all."
        printf '\n'
        return 0
    fi

    printf '  Nothing has been downloaded. Left alone, the pull fails partway through\n'
    printf '  with "no matching manifest for linux/%s/v8" and no indication of which\n' "$HOST_ARCH"
    printf '  image caused it.\n\n'
    printf '  Options:\n'
    printf '    1. Install on an amd64 host.\n'
    printf '    2. Run the amd64 builds under emulation — slower, but functional.\n'
    printf '       On Docker Desktop enable Settings -> General -> "Use Rosetta for\n'
    printf '       x86_64/amd64 emulation", then re-run:\n'
    printf '         OPENNVR_ALLOW_EMULATION=1 ./scripts/install.sh\n'
    printf '    3. Build the images above from source for this architecture.\n\n'
    die "Aborting: ${count} image(s) have no linux/${HOST_ARCH} build."
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

# ── Hardware-aware model suggestion ────────────────────────────────
# Detect the resources of the machine that will RUN the LLM and map them
# to a sensible Ollama model tier. Suggestion only — every prompt still
# lets the operator type anything. Tiers mirror
# examples/camera-agent/MODELS_AND_LATENCY.md; update both together.
#
# detect_llm_hardware <host|container> → sets HW_RAM_GB, HW_CORES, HW_ACCEL
detect_llm_hardware() {
    local where="$1"
    HW_RAM_GB=0; HW_CORES=0; HW_ACCEL="cpu"
    if [[ "$PLATFORM" == "macOS" ]]; then
        HW_RAM_GB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
        HW_CORES=$(sysctl -n hw.ncpu 2>/dev/null || echo 0)
        # Apple Silicon → Metal, but ONLY when the model runs on the host:
        # the Docker VM has no GPU access, and its RAM is the VM's
        # allowance, not the machine's.
        if [[ "$where" == "host" && "$(uname -m)" == "arm64" ]]; then
            HW_ACCEL="metal"
        fi
        if [[ "$where" == "container" ]]; then
            HW_RAM_GB=$(( $(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0) / 1073741824 ))
        fi
    else
        HW_RAM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1048576 ))
        HW_CORES=$(nproc 2>/dev/null || echo 0)
        # NVIDIA on Linux: Ollama uses CUDA both in-container (with the
        # nvidia runtime) and on the host, so it counts either way.
        if command -v nvidia-smi >/dev/null 2>&1 \
           && nvidia-smi -L >/dev/null 2>&1; then
            HW_ACCEL="cuda"
        fi
    fi
}

# suggest_llm_model → echoes the suggested Ollama model for the detected
# hardware. Two constraints shape the tiers, deliberately:
#   * FLOOR — tool-calling quality: below ~1.5b the agent misroutes
#     tools noticeably, so tiny tiers are only suggested where latency
#     would otherwise make it unusable.
#   * CEILING — the tested envelope: suggestions never exceed the
#     largest model this agent has been exercised with (qwen2.5:3b).
#     "This hardware CAN run a bigger model" is detectable; "bigger
#     will be BETTER for this agent's tool-calling and voice latency"
#     is not — 7b doubles latency for untested gain, so machines with
#     headroom get it as a note to try, never as the default.
# Staying inside one family (qwen2.5) keeps the chat template and
# tool-call format identical across tiers.
suggest_llm_model() {
    if [[ "$HW_ACCEL" != "cpu" ]]; then
        if (( HW_RAM_GB >= 16 )); then echo "qwen2.5:3b"
        else echo "qwen2.5:1.5b"; fi
    else
        if   (( HW_RAM_GB >= 16 && HW_CORES >= 8 )); then echo "qwen2.5:1.5b"
        else echo "qwen2.5:0.5b"; fi
    fi
}

# suggest_vlm_model → caption/VQA model for CAPTION_ADAPTER=ollamavlm.
# ALWAYS moondream: it is the project's tested VQA default, and the
# ollamavlm adapter is itself new — defaulting an untested model
# through a new adapter based on a RAM check would stack unknowns.
# Capable machines are told (in the prompt note) that qwen2.5vl:3b is
# worth trying; that stays an operator decision.
suggest_vlm_model() {
    echo "moondream"
}

# ── Catalog-driven model menu ───────────────────────────────────────
# Renders examples/camera-agent/model_catalog.txt (kind|model|min_ram_gb|
# tested|speed|summary) as a numbered menu, annotated for the DETECTED
# hardware, with the hardware-sized suggestion preselected. The operator
# picks a number or types any Ollama model name. Falls back to a plain
# prompt when the catalog file is missing. Sets PICKED_MODEL.
pick_model_from_catalog() {
    local kind="$1" suggest="$2" label="$3"
    local catalog="examples/camera-agent/model_catalog.txt"
    PICKED_MODEL="$suggest"
    if [[ ! -f "$catalog" ]]; then
        ask_value "$label" "$suggest"
        PICKED_MODEL="$REPLY"
        return 0
    fi
    local -a names summaries
    local line model min_ram tested speed summary idx=0 default_idx=""
    printf '\n  %s — pick a number, or type any Ollama model name:\n' "$label"
    while IFS='|' read -r k model min_ram tested speed summary; do
        [[ "$k" == "$kind" ]] || continue
        idx=$((idx + 1))
        names[$idx]="$model"
        local fit="" mark=""
        if (( HW_RAM_GB > 0 && min_ram > HW_RAM_GB )); then
            fit="  [needs ~${min_ram} GB — this machine detected ${HW_RAM_GB} GB]"
        fi
        [[ "$model" == "$suggest" ]] && { mark="  ← suggested"; default_idx="$idx"; }
        printf '   %d. %-16s ~%sGB  %-8s %-8s %s%s%s\n' \
            "$idx" "$model" "$min_ram" "$speed" \
            "$([[ "$tested" == yes ]] && echo tested || echo untested)" \
            "$summary" "$fit" "$mark"
    done < <(grep -v '^#' "$catalog")
    read -r -p "  ${label} [${default_idx:-$suggest}]: " REPLY || true
    REPLY="${REPLY:-${default_idx:-$suggest}}"
    if [[ "$REPLY" =~ ^[0-9]+$ ]] && (( REPLY >= 1 && REPLY <= idx )); then
        PICKED_MODEL="${names[$REPLY]}"
    elif [[ -n "$REPLY" ]]; then
        PICKED_MODEL="$REPLY"
    fi
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

        printf '\n'
        # ── LLM runtime: bundled container vs the host machine ─────
        # Asked BEFORE the model prompts: where the LLM runs decides
        # which hardware the model suggestion below should be sized
        # for (host GPU/RAM vs the Docker VM's CPU-only allowance).
        # Platform-aware default: on macOS the Docker VM has no GPU
        # access (no Metal for Linux guests), so the bundled container
        # answers on plain CPU — minutes per turn — while host Ollama
        # uses the Apple Silicon GPU. On Linux the container is native
        # and compose-managed: keep it the default there. A host
        # Ollama already answering on :11434 flips the default too.
        local llm_default="1" host_ollama="" llm_where="container"
        if command -v curl >/dev/null 2>&1 \
           && curl -sf --max-time 2 http://localhost:11434/api/version >/dev/null 2>&1; then
            host_ollama="yes"
        fi
        if [[ "$PLATFORM" == "macOS" || -n "$host_ollama" ]]; then
            llm_default="2"
        fi
        explain "Where should the LLM run? In Docker on macOS/Windows the container CANNOT use the GPU — answers take minutes of pure CPU. Ollama running ON this machine uses the real GPU (Metal on Apple Silicon) and skips a 3.2 GB image. On a Linux server the bundled container is fine." \
            "pick one" "$llm_default"
        if [[ -n "$host_ollama" ]]; then
            ok "Found Ollama already running on this machine (:11434)"
        fi
        ask_value "LLM runtime: 1=bundled container, 2=Ollama on this machine / external URL" "$llm_default"
        if [[ "$REPLY" == "2" ]]; then
            llm_where="host"
            configure_value OLLAMA_EXTERNAL_URL "External LLM endpoint" "http://host.docker.internal:11434" \
                "Ollama-compatible endpoint the agent calls for the LLM. host.docker.internal reaches this machine from inside Docker." "yes" \
                "Native Ollama: http://host.docker.internal:11434 | LAN box: http://<ip>:11434"
            # Offer to install / start right here, so "external" never
            # means "broken until you read the docs".
            if [[ -z "$host_ollama" ]]; then
                if command -v ollama >/dev/null 2>&1; then
                    warn "Ollama is installed but not answering on :11434 — start it"
                    warn "(macOS: open the Ollama app, or: brew services start ollama)."
                elif [[ "$PLATFORM" == "macOS" ]] && command -v brew >/dev/null 2>&1; then
                    if ask_yes_no "Ollama is not installed. Install it now with Homebrew?" y; then
                        { brew install ollama && brew services start ollama \
                            && ok "Ollama installed and started."; } \
                            || warn "Install failed — get it from https://ollama.com/download"
                    else
                        warn "Install Ollama before first use: https://ollama.com/download"
                    fi
                else
                    warn "Install Ollama on this machine first: https://ollama.com/download"
                fi
            fi
            info "The bundled ollama container will be skipped entirely."
        else
            env_set OLLAMA_EXTERNAL_URL ""
        fi

        # ── Hardware-aware model suggestions ───────────────────────
        # Size the defaults for the machine that will actually run the
        # model. Suggestions only — type any Ollama model to override.
        detect_llm_hardware "$llm_where"
        local llm_suggest vlm_suggest hw_desc
        llm_suggest=$(suggest_llm_model)
        vlm_suggest=$(suggest_vlm_model)
        hw_desc="${HW_RAM_GB} GB RAM, ${HW_CORES} cores"
        case "$HW_ACCEL" in
            metal) hw_desc="Apple Silicon GPU (Metal), $hw_desc" ;;
            cuda)  hw_desc="NVIDIA GPU (CUDA), $hw_desc" ;;
            *)     hw_desc="CPU only, $hw_desc" ;;
        esac
        if [[ "$llm_where" == "container" && "$PLATFORM" == "macOS" ]]; then
            hw_desc="$hw_desc (Docker VM allowance)"
        fi
        ok "Detected: $hw_desc → suggesting $llm_suggest"

        printf '\n  ── Camera Agent models (all local, no API keys) ───────\n'
        explain "The local chat model that answers your questions; must support tool calling. The suggestion is sized for this machine ($hw_desc), capped at the largest model this agent is tested with — 'untested' entries are known-good models nobody has validated with THIS agent yet." \
            "yes" "$llm_suggest"
        pick_model_from_catalog llm "$llm_suggest" "Local LLM model (Ollama)"
        env_set OLLAMA_MODEL "$PICKED_MODEL"
        # Pull offer AFTER the model choice, so it pulls what was chosen.
        if [[ "$llm_where" == "host" ]]; then
            local ext_model
            ext_model=$(env_get OLLAMA_MODEL); ext_model=${ext_model:-$llm_suggest}
            if command -v ollama >/dev/null 2>&1; then
                if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$ext_model"; then
                    ok "Model ${ext_model} is already available on this machine."
                elif ask_yes_no "Pull the model now (ollama pull ${ext_model})?" y; then
                    ollama pull "$ext_model" \
                        || warn "Pull failed — run 'ollama pull ${ext_model}' manually before first use."
                else
                    warn "Before first use:  ollama pull ${ext_model}"
                fi
            else
                warn "Before first use, pull the model ON THE HOST:  ollama pull ${ext_model}"
            fi
        fi
        if [[ "$EXAMPLE_PROFILE" == "camera-agent" ]]; then
            configure_value WHISPER_MODEL_SIZE "Whisper speech-to-text model" "base.en" \
                "Transcribes your spoken questions (voice mode only)." "yes" \
                "tiny.en (fastest) | base.en (default) | small.en (most accurate)."
        fi
        configure_value CAPTION_ADAPTER "Scene-description model" "moondream" \
            "Describes what a camera sees. moondream answers questions (VQA); blip writes plain captions; ollamavlm proxies to your Ollama (GPU-fast on macOS when the LLM runs on this machine — needs an adapter tag newer than 0.1.3)." "yes" \
            "moondream | blip | ollamavlm — all local."
        # ollamavlm chosen → suggest a VLM sized like the LLM was.
        if [[ "$(env_get CAPTION_ADAPTER)" == "ollamavlm" ]]; then
            explain "Multimodal Ollama model the ollamavlm adapter uses for scene questions; the adapter auto-pulls it. moondream is the tested default." \
                "yes" "$vlm_suggest"
            pick_model_from_catalog vlm "$vlm_suggest" "Vision model (Ollama)"
            env_set OLLAMA_VLM_MODEL "$PICKED_MODEL"
        fi
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

# ── Docker VM allowance check (macOS; Windows equivalent in install.ps1) ──
# On macOS/Windows every container shares ONE VM whose CPU/RAM allowance
# is a Docker Desktop setting — often left at a small default (half the
# cores, a few GB). detect-pipeline, the caption VLM, and postgres all
# live inside it. We CANNOT change that setting from here (it belongs to
# Docker Desktop and needs an Apply & Restart); what we can do is notice
# it is undersized for what was just selected and say exactly what to
# change. Thresholds: 6 GB / 4 CPUs for the core stack, 8 GB when a
# camera-agent example was selected with the BUNDLED LLM (the container
# LLM is the one big in-VM RAM consumer; with an external LLM the core
# threshold applies).
check_docker_vm_allowance() {
    [[ "$PLATFORM" == "macOS" ]] || return 0
    local vm_mem_gb vm_cpus host_mem_gb host_cpus
    vm_mem_gb=$(( $(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0) / 1073741824 ))
    vm_cpus=$(docker info --format '{{.NCPU}}' 2>/dev/null || echo 0)
    host_mem_gb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
    host_cpus=$(sysctl -n hw.ncpu 2>/dev/null || echo 0)
    (( vm_mem_gb > 0 && vm_cpus > 0 )) || return 0

    local need_mem=6
    if [[ -n "$EXAMPLE_NAME" && -z "$(env_get OLLAMA_EXTERNAL_URL)" ]]; then
        need_mem=8
    fi
    info "Docker VM allowance: ${vm_cpus} CPUs / ${vm_mem_gb} GB (this machine has ${host_cpus} CPUs / ${host_mem_gb} GB)."
    if (( vm_mem_gb < need_mem || vm_cpus < 4 )); then
        warn "That is on the small side for what you selected (recommended: ≥4 CPUs, ≥${need_mem} GB)."
        warn "OpenNVR cannot change this itself — raise it in Docker Desktop:"
        warn "  Settings → Resources → CPUs / Memory → Apply & restart,"
        warn "then re-run ./start.sh up. Containers share this allowance;"
        warn "detect-pipeline and the vision model are the main consumers."
        if (( host_mem_gb >= 16 )); then
            info "With ${host_mem_gb} GB in this machine, giving Docker $(( host_mem_gb / 2 )) GB is a safe choice."
        fi
    fi
}

pull_and_build() {
    printf '\n'
    info "First-time setup downloads several container images (and, for the"
    info "Camera Agent, a local LLM model of ~1 GB). Depending on your network"
    info "this can take 8-15 minutes. Later starts are much faster — everything"
    info "is cached, so you only pay this cost once."
    check_image_architectures
    printf '\n'
    info "Pulling the OpenNVR core stack..."
    docker compose -f "$BASE_COMPOSE" pull --ignore-buildable

    choose_example
    check_docker_vm_allowance
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