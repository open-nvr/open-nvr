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
        elif [[ "${NVIDIA_REBOOT_PENDING:-}" == "yes" ]]; then
            # The driver is installed but not LOADED until this box
            # reboots, so nvidia-smi answers nothing. Sizing for the CPU
            # here bakes a small model into .env for a machine one reboot
            # away from a GPU — the field report this came from had a
            # 30 GB CUDA box suggesting qwen3:1.7b.
            HW_ACCEL="cuda"
        fi
    fi
}

# ── Sizing the models to the machine ───────────────────────────────
# Both Ollama models are RESIDENT AT ONCE (the shipped compose sets
# OLLAMA_KEEP_ALIVE=-1 so answers stay snappy), and they share the box with
# the OpenNVR stack itself. So what has to fit is the SUM, not either model
# on its own — sizing them independently is what put a 4 GB vision model on
# 8 GB machines and left them thrashing.
#
# Measured on a running install: the 13 stack containers hold ~2 GB RSS, and
# Docker Desktop adds its VM overhead on top of that. Leave the operator's
# own machine some room too — a desktop with zero free RAM is a broken one.
OPENNVR_STACK_GB=3
OPENNVR_OS_HEADROOM_GB=2

# compute_model_budget → sets HW_MODEL_BUDGET_GB, the RAM left for models.
compute_model_budget() {
    HW_MODEL_BUDGET_GB=$(( HW_RAM_GB - OPENNVR_STACK_GB - OPENNVR_OS_HEADROOM_GB ))
    (( HW_MODEL_BUDGET_GB < 0 )) && HW_MODEL_BUDGET_GB=0
    return 0
}

# speed_rank <speed> → 0 (fastest) .. 4 (slowest). The catalog's speed column
# is editorial prose; this is the only place that turns it into an ordering.
speed_rank() {
    case "$1" in
        fastest) echo 0 ;; fast) echo 1 ;; medium) echo 2 ;;
        slower)  echo 3 ;; slowest) echo 4 ;; *) echo 2 ;;
    esac
}

# catalog_pick <kind> <max_ram_gb> [max_speed_rank] → largest TESTED model of
# that kind that fits BOTH ceilings, or empty. Reads the catalog so the tiers
# can never drift from the menu the operator is shown.
catalog_pick() {
    local kind="$1" cap="$2" speed_cap="${3:-9}"
    local catalog="examples/camera-agent/model_catalog.txt"
    local k model min_ram tested speed rest best="" best_ram=-1
    [[ -f "$catalog" ]] || return 0
    while IFS='|' read -r k model min_ram tested speed rest; do
        [[ "$k" == "$kind" && "$tested" == "yes" ]] || continue
        (( min_ram <= cap )) || continue
        (( $(speed_rank "$speed") <= speed_cap )) || continue
        (( min_ram > best_ram )) && { best="$model"; best_ram="$min_ram"; }
    done < <(grep -v '^#' "$catalog")
    printf '%s|%s' "$best" "$best_ram"
}

# suggest_models → sets SUGGEST_LLM, SUGGEST_VLM, SUGGEST_CAPTION_ADAPTER.
#
# Two ceilings, deliberately:
#   * SPEED — a model that fits in RAM can still be unusable. CPU-only boxes
#     get a tier matched to their cores, because "it fits" and "it answers
#     before the operator gives up" are different questions.
#   * TESTED — never suggest past the envelope this agent has actually been
#     exercised with. "This machine CAN run a bigger model" is detectable;
#     "bigger will be BETTER at tool-calling and voice latency" is not, so
#     untested headroom stays a note in the menu, never a default.
#
# The tool-routing floor still applies: below ~1.5b the agent misroutes tools
# noticeably, so the tiny tier is only chosen where nothing else fits.
suggest_models() {
    compute_model_budget
    local cap_llm
    if [[ "$HW_ACCEL" != "cpu" ]]; then cap_llm=4        # GPU: up to qwen2.5:3b
    elif (( HW_CORES >= 8 )); then      cap_llm=3        # strong CPU: qwen3:1.7b
    elif (( HW_CORES >= 4 )); then      cap_llm=2        # modest CPU: qwen2.5:1.5b
    else                                cap_llm=1        # weak CPU: the tiny tier
    fi
    (( cap_llm > HW_MODEL_BUDGET_GB )) && cap_llm=$HW_MODEL_BUDGET_GB

    local picked llm_ram
    picked=$(catalog_pick llm "$cap_llm")
    SUGGEST_LLM="${picked%%|*}"; llm_ram="${picked##*|}"
    if [[ -z "$SUGGEST_LLM" ]]; then
        # Nothing fits the budget. Suggest the smallest tested model anyway —
        # the operator may know something we do not (swap, a spare box, an
        # external Ollama) — and let the menu's own fit warning speak.
        SUGGEST_LLM="qwen2.5:0.5b"; llm_ram=1
    fi

    # Whatever the LLM did not take. A vision model this machine cannot hold
    # is worse than a smaller one: it evicts the LLM on every question.
    local remaining=$(( HW_MODEL_BUDGET_GB - llm_ram ))
    (( remaining < 0 )) && remaining=0
    # On a weak CPU, serving vision through Ollama is slow enough to be a
    # worse experience than the small in-container model, whatever fits.
    if [[ "$HW_ACCEL" == "cpu" ]] && (( HW_CORES < 4 )); then remaining=0; fi
    # Vision gets a SPEED ceiling too, not just a RAM one — the same rule the
    # LLM already got. Without it every machine with room landed on the same
    # 4B model: it fits, so it wins, even CPU-only where a caption costs ~25 s
    # against ~1-2 s on a GPU. Fitting in RAM and answering before the
    # operator gives up are different questions.
    local vlm_speed_cap=9
    [[ "$HW_ACCEL" == "cpu" ]] && vlm_speed_cap=1        # CPU: the fast tier only
    picked=$(catalog_pick vlm "$remaining" "$vlm_speed_cap")
    SUGGEST_VLM="${picked%%|*}"
    if [[ -n "$SUGGEST_VLM" ]]; then
        SUGGEST_CAPTION_ADAPTER="ollamavlm"
    else
        # No room to serve vision from Ollama: fall back to the moondream
        # ADAPTER, a ~0.5b-int8 build in its own container — far smaller than
        # anything in the Ollama catalog, and still a real VQA answer.
        SUGGEST_CAPTION_ADAPTER="moondream"
        SUGGEST_VLM=""
    fi
}

# suggest_whisper_model → speech-to-text sized to the CPU that will run it.
# Transcription is on the voice critical path: a model that takes four
# seconds to transcribe "is anyone at the door" makes the agent feel broken,
# so weak machines get the fast tier and strong ones the accurate one.
suggest_whisper_model() {
    if   (( HW_CORES >= 8 && HW_RAM_GB >= 16 )); then echo "small.en"
    elif (( HW_CORES >= 4 )); then                    echo "base.en"
    else                                              echo "tiny.en"; fi
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
    local -a names
    local line model min_ram tested speed summary idx=0 default_idx=""
    printf '\n  %s — pick a number, or type any Ollama model name:\n' "$label"
    while IFS='|' read -r k model min_ram tested speed summary; do
        [[ "$k" == "$kind" ]] || continue
        idx=$((idx + 1))
        names[$idx]="$model"
        local fit="" mark=""
        if (( HW_RAM_GB > 0 && min_ram > HW_MODEL_BUDGET_GB )); then
            # Against the BUDGET, not total RAM: a 4 GB model on an 8 GB box
            # "fits" only if you ignore the stack and the other model.
            fit="  [needs ~${min_ram} GB — only ~${HW_MODEL_BUDGET_GB} GB free for models]"
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

# --- ollama runtime helpers (tested: test_installer_ollama_gate.sh) ---
# One probe, one waiter, one starter. These used to be five copy-pasted
# curl lines and — worse — "the binary is on PATH" was accepted as "the
# runtime is up". That is how a finished install ended up pointing the
# agent at a :11434 that answered nothing. The rule now lives in one
# place: ONLY A LIVE ENDPOINT COUNTS.
OLLAMA_LOCAL_URL="http://localhost:11434"

ollama_endpoint_up() {
    curl -sf --max-time 2 "${OLLAMA_LOCAL_URL}/api/version" >/dev/null 2>&1
}

# ollama_installed → is the binary on PATH? Answers "should we offer to
# install it", and nothing else. Never "is it working".
ollama_installed() {
    command -v ollama >/dev/null 2>&1
}

# wait_for_ollama <seconds> — poll until it answers. Prints progress: a
# silent 60-second wait is indistinguishable from a hung installer, and
# the old 10-second SILENT poll declared defeat while the service was
# still coming up (reported from the field on a CUDA box).
wait_for_ollama() {
    local secs="${1:-60}" waited=0
    if ollama_endpoint_up; then
        return 0
    fi
    printf '  Waiting for Ollama to answer on :11434 (up to %ss)' "$secs"
    while (( waited < secs )); do
        sleep 2
        waited=$((waited + 2))
        printf '.'
        if ollama_endpoint_up; then
            printf ' up after %ss\n' "$waited"
            return 0
        fi
    done
    printf ' still silent\n'
    return 1
}

# try_start_ollama — actually start the service rather than printing a
# hint and hoping. Returns 0 only when a start was attempted and worked.
try_start_ollama() {
    if [[ "$PLATFORM" == "macOS" ]]; then
        if command -v brew >/dev/null 2>&1; then
            info "Starting Ollama (brew services start ollama)..."
            if brew services start ollama >/dev/null 2>&1; then
                return 0
            fi
        fi
        info "Starting the Ollama app..."
        open -a Ollama >/dev/null 2>&1
        return
    fi
    command -v systemctl >/dev/null 2>&1 || return 1
    if ask_yes_no "Start Ollama now (sudo systemctl start ollama)?" y; then
        if sudo systemctl start ollama; then
            return 0
        fi
        warn "systemctl start ollama failed."
    fi
    return 1
}

# ollama_start_diagnosis — what to look at when it will not come up. The
# old message ("start it: sudo systemctl start ollama") was wrong exactly
# when it mattered: the operator had already started it.
ollama_start_diagnosis() {
    if [[ "$PLATFORM" == "macOS" ]]; then
        warn "Open the Ollama app (or: brew services start ollama)."
        return 0
    fi
    if command -v systemctl >/dev/null 2>&1; then
        warn "ollama.service is: $(systemctl is-active ollama 2>/dev/null || echo unknown)"
        warn "See why:  journalctl -u ollama -n 20 --no-pager"
    else
        warn "Start it with:  ollama serve"
    fi
    return 0
}

# --- pending NVIDIA driver reboot (Linux) --------------------------
# Ollama's official installer installs a CUDA driver when it finds an
# NVIDIA card without one, and signs off with ">>> Reboot to complete
# NVIDIA CUDA driver install." Until that reboot nvidia-smi cannot talk
# to the GPU — so detect_llm_hardware read a 30 GB CUDA box as CPU-only
# and wrote a CPU-tier model into .env for a machine one reboot away
# from running something far bigger. That is the bug this flag exists
# for.
NVIDIA_REBOOT_PENDING=""

nvidia_hardware_present() {
    [[ "$PLATFORM" == "macOS" ]] && return 1
    if command -v lspci >/dev/null 2>&1; then
        if lspci 2>/dev/null | grep -qi nvidia; then
            return 0
        fi
    fi
    # No pciutils on a minimal server — ask the PCI bus directly.
    # 0x10de is NVIDIA's vendor id.
    grep -qil 0x10de /sys/bus/pci/devices/*/vendor 2>/dev/null
}

nvidia_driver_live() {
    command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1
}

# detect_nvidia_reboot_pending [installer-log] → sets NVIDIA_REBOOT_PENDING.
# Two signals: the installer's own wording, and the state it leaves
# behind (a card on the bus with no driver answering). The second is the
# backstop for the first — Ollama can reword its output, and the state
# is the thing that actually matters. Called ONLY right after Ollama's
# installer runs: on any other machine "an NVIDIA card with no live
# driver" could as easily mean nobody ever installed one, and telling
# that operator to reboot would be a guess.
detect_nvidia_reboot_pending() {
    local log="${1:-}"
    [[ "$PLATFORM" == "macOS" ]] && return 0
    if [[ -n "$log" && -f "$log" ]] && grep -qi "reboot to complete" "$log"; then
        NVIDIA_REBOOT_PENDING="yes"
    elif nvidia_hardware_present && ! nvidia_driver_live; then
        NVIDIA_REBOOT_PENDING="yes"
    fi
    return 0
}

# nvidia_reboot_notice — printed at detection AND again as the last
# thing before the stack starts. main() ends in `exec start.sh up`,
# after which this script never gets another word in, and compose output
# would bury a single early warning several screens up.
nvidia_reboot_notice() {
    printf '\n'
    warn "────────────────────────────────────────────────────────"
    warn "NVIDIA driver installed — THIS MACHINE NEEDS A REBOOT."
    warn ""
    warn "Until you reboot, nvidia-smi cannot see the GPU, so Ollama"
    warn "answers on the CPU or fails to start at all."
    warn ""
    warn "The models below are sized for the GPU you will have AFTER"
    warn "the reboot, not for the CPU-only machine this looks like"
    warn "right now."
    warn ""
    warn "When this finishes:"
    warn "    sudo reboot"
    warn "    cd ${PROJECT_ROOT} && ./start.sh up"
    warn "Every answer is already saved in .env — nothing to redo."
    warn "────────────────────────────────────────────────────────"
    printf '\n'
}

# check_ollama_bridge_reachable — probe :11434 from the address the
# CONTAINER will use, not from this shell.
#
# A host-local probe is not the test that matters. The agent runs in a
# container and reaches this machine through host.docker.internal, which
# on Linux Docker Engine is the bridge gateway (the `host-gateway` entry
# in docker-compose.camera-agent.yml). Ollama's Linux service binds
# 127.0.0.1, which answers localhost and REFUSES that gateway: a green
# install that fails on the very first question. Docker Desktop proxies
# host.docker.internal to the host loopback, so this is Linux Docker
# Engine's problem alone.
check_ollama_bridge_reachable() {
    local gw
    gw=$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null || true)
    [[ -n "$gw" ]] || return 0
    if curl -sf --max-time 3 "http://${gw}:11434/api/version" >/dev/null 2>&1; then
        ok "Docker can reach Ollama at ${gw}:11434 — the address the agent uses."
        return 0
    fi
    warn "Ollama answers on localhost but REFUSES ${gw}:11434, which is what"
    warn "host.docker.internal resolves to inside the container. The agent"
    warn "would fail its first question with 'connection refused'."
    warn "Cause: Ollama's Linux service binds 127.0.0.1 by default."
    info ""
    info "The fix — a systemd drop-in that binds every interface:"
    info "    sudo mkdir -p /etc/systemd/system/ollama.service.d"
    info "    sudo tee /etc/systemd/system/ollama.service.d/10-opennvr-host.conf <<CONF"
    info "    [Service]"
    info '    Environment="OLLAMA_HOST=0.0.0.0:11434"'
    info "    CONF"
    info "    sudo systemctl daemon-reload && sudo systemctl restart ollama"
    info ""
    if ! ask_yes_no "Apply that now?" y; then
        warn "Skipped — apply it before first use, or the agent cannot reach Ollama."
        return 0
    fi
    if ! { sudo mkdir -p /etc/systemd/system/ollama.service.d \
           && printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' \
              | sudo tee /etc/systemd/system/ollama.service.d/10-opennvr-host.conf >/dev/null \
           && sudo systemctl daemon-reload \
           && sudo systemctl restart ollama; }; then
        warn "Could not apply the drop-in — run the commands above by hand."
        return 0
    fi
    wait_for_ollama 30 || true
    if curl -sf --max-time 3 "http://${gw}:11434/api/version" >/dev/null 2>&1; then
        ok "Docker can now reach Ollama at ${gw}:11434."
    else
        warn "Still refused from ${gw}:11434 — the bind was not the only cause."
        warn "Check the host firewall:  sudo ufw allow in on docker0"
    fi
    return 0
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

    # Default to the Camera Agent: it is the example this installer is
    # built around -- the mode, LLM-runtime and model prompts below all
    # exist to serve it -- and the only one shipping a Compose manifest
    # today, so a default of 0 made Enter decline the very thing this
    # section had just finished pitching. Its NUMBER moves whenever an
    # example directory is added, so look it up by name; if it is ever
    # missing or unshippable the default falls back to 0 (core only).
    local default_choice=0 mark
    index=1
    for name in "${names[@]}"; do
        if [[ "$name" == "camera-agent" ]] && find_example_compose "$name" >/dev/null; then
            default_choice="$index"
        fi
        index=$((index + 1))
    done

    printf '\n  Available examples:\n'
    index=1
    for name in "${names[@]}"; do
        mark=""
        if (( index == default_choice )); then
            mark="  <- default"
        fi
        if manifest=$(find_example_compose "$name"); then
            printf '  %2d. %-30s [installable: %s]%s\n' "$index" "$name" "$manifest" "$mark"
        else
            printf '  %2d. %-30s [no Compose manifest]\n' "$index" "$name"
        fi
        index=$((index + 1))
    done
    printf '   0. Core stack only\n\n'
    read -r -p "  Select an example [$default_choice]: " choice || true  # EOF-safe under set -e (see ask_* note)
    choice="${choice:-$default_choice}"
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
        # Host Ollama is the default on EVERY platform. On macOS and
        # Windows the Docker VM has no GPU access (no Metal/CUDA for
        # Linux guests), so the bundled container answers on plain CPU
        # — minutes per turn. On Linux the container can reach the GPU,
        # but only once the host toolkit is wired up, and it still
        # costs a 3.2 GB image plus a second model store beside the
        # Ollama the box likely already has. Host Ollama is the answer
        # that is right on all three, and the gate below installs and
        # starts it when it is missing rather than leaving a broken URL.
        local llm_default="2" host_ollama="" llm_where="container"
        if command -v curl >/dev/null 2>&1 \
           && curl -sf --max-time 2 http://localhost:11434/api/version >/dev/null 2>&1; then
            host_ollama="yes"
        fi
        explain "Where should the LLM run? Ollama running ON this machine uses the real GPU (Metal on Apple Silicon, CUDA on Linux) and skips a 3.2 GB image — the installer offers to set it up if you do not have it. In Docker on macOS/Windows the container CANNOT use the GPU at all: answers take minutes of pure CPU. Pick the bundled container only if you want the LLM compose-managed with everything else." \
            "pick one" "$llm_default (Ollama on this machine)"
        if [[ -n "$host_ollama" ]]; then
            ok "Found Ollama already running on this machine (:11434)"
        fi
        ask_value "LLM runtime: 1=bundled container, 2=Ollama on this machine / external URL" "$llm_default"
        if [[ "$REPLY" == "2" ]]; then
            llm_where="host"
            configure_value OLLAMA_EXTERNAL_URL "External LLM endpoint" "http://host.docker.internal:11434" \
                "Ollama-compatible endpoint the agent calls for the LLM. host.docker.internal reaches this machine from inside Docker." "yes" \
                "Native Ollama: http://host.docker.internal:11434 | LAN box: http://<ip>:11434"
            # --- ollama-availability gate (tested: test_installer_ollama_gate.sh) ---
            # Offer to install / start right here, so "external" never
            # means "broken until you read the docs". Warning-and-proceeding
            # is not enough: seen in the field on Linux, the installer
            # printed the download URL, carried on, and produced a finished
            # install whose agent pointed at :11434 with nothing listening.
            #
            # THE RULE: only a live endpoint counts. An earlier version of
            # this gate accepted `command -v ollama` as a runtime and
            # shipped exactly the dead-endpoint install it exists to
            # prevent — with a louder warning attached. The binary being on
            # PATH decides whether to OFFER an install; it never decides
            # whether the LLM works.
            if [[ -z "$host_ollama" ]]; then
                local ext_url ollama_ready=""
                ext_url=$(env_get OLLAMA_EXTERNAL_URL)
                case "$ext_url" in
                    ""|*host.docker.internal*|*localhost*|*127.0.0.1*)
                        # The endpoint is THIS machine — a local Ollama is
                        # genuinely required.
                        if ollama_installed; then
                            warn "Ollama is installed but not answering on :11434."
                            if try_start_ollama && wait_for_ollama 60; then
                                ok "Ollama is answering on :11434."
                                ollama_ready="yes"
                            else
                                ollama_start_diagnosis
                            fi
                        elif [[ "$PLATFORM" == "macOS" ]] && command -v brew >/dev/null 2>&1; then
                            if ask_yes_no "Ollama is not installed. Install it now with Homebrew?" y; then
                                if brew install ollama && brew services start ollama \
                                   && wait_for_ollama 60; then
                                    ok "Ollama installed and answering on :11434."
                                    ollama_ready="yes"
                                else
                                    warn "Install did not produce a live endpoint — https://ollama.com/download"
                                fi
                            fi
                        elif [[ "$PLATFORM" != "macOS" ]] && command -v curl >/dev/null 2>&1; then
                            if ask_yes_no "Ollama is not installed. Install it now (Ollama's official installer: curl -fsSL https://ollama.com/install.sh | sh)?" y; then
                                # Download THEN run — never `curl | sh` for the
                                # verdict: when curl fails, sh reads empty input
                                # and exits 0, so a dead network reads as a
                                # successful install and we ship the exact
                                # broken state this gate exists to prevent.
                                local _tmp _log
                                _tmp=$(mktemp); _log=$(mktemp)
                                # tee, not a bare run: the operator still watches
                                # it live AND we get to read what it said. Under
                                # `set -o pipefail` the pipeline still reports
                                # sh's real exit status, not tee's.
                                if curl -fsSL https://ollama.com/install.sh -o "$_tmp" \
                                   && sh "$_tmp" 2>&1 | tee "$_log"; then
                                    # ">>> Reboot to complete NVIDIA CUDA driver
                                    # install." is the line that made a 30 GB
                                    # CUDA box size itself as CPU-only.
                                    detect_nvidia_reboot_pending "$_log"
                                    rm -f "$_tmp" "$_log"
                                    if wait_for_ollama 60; then
                                        ok "Ollama installed and answering on :11434."
                                        ollama_ready="yes"
                                    elif ollama_installed \
                                         && try_start_ollama && wait_for_ollama 60; then
                                        ok "Ollama is answering on :11434."
                                        ollama_ready="yes"
                                    elif ollama_installed; then
                                        warn "Ollama is installed but will not answer on :11434."
                                        ollama_start_diagnosis
                                    else
                                        warn "The installer ran but ollama is still not available."
                                    fi
                                else
                                    rm -f "$_tmp" "$_log"
                                    warn "Install failed — get it from https://ollama.com/download"
                                fi
                            fi
                        fi
                        # A pending NVIDIA driver reboot is the one known
                        # cause of a dead endpoint that has a known remedy:
                        # the service comes up once the box reboots. Falling
                        # back to the bundled container here would hand a GPU
                        # machine the CPU runtime permanently. This is the
                        # ONLY way past this gate without a live endpoint,
                        # and it still takes an explicit yes.
                        if [[ -z "$ollama_ready" && "${NVIDIA_REBOOT_PENDING:-}" == "yes" ]]; then
                            nvidia_reboot_notice
                            if ask_yes_no "Keep Ollama on this machine (it starts after the reboot)?" y; then
                                ollama_ready="yes"
                            fi
                        fi
                        if [[ -z "$ollama_ready" ]]; then
                            # No local Ollama and none installed. Proceeding
                            # as-is ships an agent wired to a dead endpoint —
                            # the honest default is the runtime that works.
                            if ask_yes_no "Without Ollama on this machine that endpoint answers nothing. Use the bundled ollama container instead?" y; then
                                llm_where="container"
                                env_set OLLAMA_EXTERNAL_URL ""
                                ok "Switched to the bundled ollama container."
                            fi
                        fi
                        # The installer does NOT proceed without a working LLM
                        # runtime (operator decision, from the field). The only
                        # ways past this point are a verified Ollama here or an
                        # explicit switch to the bundled container — the first
                        # version of this gate kept a warn-and-continue exit,
                        # which was the original bug wearing a louder warning.
                        while [[ "$llm_where" == "host" && -z "$ollama_ready" ]]; do
                            local _resp=""
                            if ! read -r -p "  Install Ollama (https://ollama.com/download) in another terminal, then press Enter to re-check — or type 'container' to use the bundled runtime: " _resp; then
                                # Stdin ran dry (unattended run): hanging
                                # forever helps no one, and proceeding broken
                                # is the bug this gate exists to prevent.
                                # Take the runtime that works.
                                warn "No interactive input — using the bundled ollama container."
                                _resp="container"
                            fi
                            case "$_resp" in
                                container|c|C)
                                    llm_where="container"
                                    env_set OLLAMA_EXTERNAL_URL ""
                                    ok "Switched to the bundled ollama container."
                                    ;;
                                *)
                                    if ollama_endpoint_up; then
                                        ok "Ollama is answering on :11434."
                                        ollama_ready="yes"
                                    elif ollama_installed; then
                                        # Installed but silent: try to START it
                                        # rather than tell the operator to. The
                                        # old branch called this "ready" and
                                        # walked out of the loop with a dead
                                        # endpoint.
                                        warn "Ollama is installed but still not answering on :11434."
                                        if try_start_ollama && wait_for_ollama 60; then
                                            ok "Ollama is answering on :11434."
                                            ollama_ready="yes"
                                        else
                                            ollama_start_diagnosis
                                        fi
                                    else
                                        warn "Ollama is still not installed."
                                    fi
                                    ;;
                            esac
                        done
                        ;;
                    *)
                        # A remote endpoint (another box on the LAN). A local
                        # Ollama is irrelevant — do not nag about installing
                        # one; just check the endpoint actually answers.
                        if command -v curl >/dev/null 2>&1 \
                           && ! curl -sf --max-time 3 "${ext_url%/}/api/version" >/dev/null 2>&1; then
                            warn "Remote LLM endpoint ${ext_url} is not answering right now —"
                            warn "make sure it is up (and reachable from Docker) before first use."
                        fi
                        ;;
                esac
            fi
            # Reachability from the CONTAINER, not from this shell. Runs on
            # the already-running path too (host_ollama=yes skips everything
            # above) — that operator's bind is exactly as suspect, and it is
            # the commonest way to reach this point.
            if [[ "$llm_where" == "host" && "$PLATFORM" != "macOS" ]]; then
                case "$(env_get OLLAMA_EXTERNAL_URL)" in
                    ""|*host.docker.internal*|*localhost*|*127.0.0.1*)
                        if ollama_endpoint_up; then
                            check_ollama_bridge_reachable
                        fi
                        ;;
                    *) ;;   # a LAN box: not ours to probe or fix
                esac
            fi
            # --- end ollama-availability gate ---
        fi
        if [[ "$llm_where" == "host" ]]; then
            info "The bundled ollama container will be skipped entirely."
        else
            env_set OLLAMA_EXTERNAL_URL ""
        fi

        # ── Hardware-aware model suggestions ───────────────────────
        # Size the defaults for the machine that will actually run the
        # model. Suggestions only — type any Ollama model to override.
        detect_llm_hardware "$llm_where"
        suggest_models
        local llm_suggest vlm_suggest hw_desc
        llm_suggest="$SUGGEST_LLM"
        vlm_suggest="$SUGGEST_VLM"
        hw_desc="${HW_RAM_GB} GB RAM, ${HW_CORES} cores"
        case "$HW_ACCEL" in
            metal) hw_desc="Apple Silicon GPU (Metal), $hw_desc" ;;
            cuda)
                # Do not claim a GPU this box cannot use yet — say when.
                if [[ "${NVIDIA_REBOOT_PENDING:-}" == "yes" ]]; then
                    hw_desc="NVIDIA GPU (CUDA, after reboot), $hw_desc"
                else
                    hw_desc="NVIDIA GPU (CUDA), $hw_desc"
                fi
                ;;
            *)     hw_desc="CPU only, $hw_desc" ;;
        esac
        if [[ "$llm_where" == "container" && "$PLATFORM" == "macOS" ]]; then
            hw_desc="$hw_desc (Docker VM allowance)"
        fi
        ok "Detected: $hw_desc → ~${HW_MODEL_BUDGET_GB} GB for models after the stack"
        if [[ -n "$vlm_suggest" ]]; then
            ok "Suggesting ${llm_suggest} + ${vlm_suggest} (both stay resident)"
        else
            ok "Suggesting ${llm_suggest}; too little left for an Ollama vision model, so scene description falls back to the small in-container moondream"
        fi

        printf '\n  ── Camera Agent models (all local, no API keys) ───────\n'
        explain "The local chat model that answers your questions; must support tool calling. The suggestion is sized for this machine ($hw_desc), capped at the largest model this agent is tested with — 'untested' entries are known-good models nobody has validated with THIS agent yet." \
            "yes" "$llm_suggest"
        pick_model_from_catalog llm "$llm_suggest" "Local LLM model (Ollama)"
        env_set OLLAMA_MODEL "$PICKED_MODEL"
        # Pull offer AFTER the model choice, so it pulls what was chosen.
        if [[ "$llm_where" == "host" ]]; then
            local ext_model
            ext_model=$(env_get OLLAMA_MODEL); ext_model=${ext_model:-$llm_suggest}
            # Gate on the ENDPOINT, not the binary: `ollama pull` needs a
            # running server, so with a dead :11434 (a pending driver reboot,
            # say) the binary check only bought a noisy failure.
            if ollama_endpoint_up; then
                if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$ext_model"; then
                    ok "Model ${ext_model} is already available on this machine."
                elif ask_yes_no "Pull the model now (ollama pull ${ext_model})?" y; then
                    ollama pull "$ext_model" \
                        || warn "Pull failed — run 'ollama pull ${ext_model}' manually before first use."
                else
                    warn "Before first use:  ollama pull ${ext_model}"
                fi
            else
                if [[ "${NVIDIA_REBOOT_PENDING:-}" == "yes" ]]; then
                    warn "Ollama is not answering yet. AFTER THE REBOOT, pull the model:"
                else
                    warn "Ollama is not answering, so nothing can be pulled yet."
                    warn "Before first use, ON THIS MACHINE:"
                fi
                warn "    ollama pull ${ext_model}"
                if [[ "$(env_get CAPTION_ADAPTER)" == "ollamavlm" ]]; then
                    warn "    ollama pull $(env_get OLLAMA_VLM_MODEL)"
                fi
            fi
        fi
        if [[ "$EXAMPLE_PROFILE" == "camera-agent" ]]; then
            configure_value WHISPER_MODEL_SIZE "Whisper speech-to-text model" \
                "$(suggest_whisper_model)" \
                "Transcribes your spoken questions (voice mode only). Sized for this machine — transcription is on the voice critical path, so weak boxes get the fast tier." "yes" \
                "tiny.en (fastest) | base.en (balanced) | small.en (most accurate)."
        fi
        # When the LLM already runs on the host Ollama, the VLM belongs there
        # too: the ollamavlm adapter proxies scene questions to the same
        # GPU-fast Ollama (a caption costs ~1-2 s on Metal vs ~25 s of VM
        # CPU with the in-container weights). Same adapter contract, same
        # audited KAI-C path — only where the weights execute changes. On
        # the bundled-container path the in-VM moondream stays the default
        # (there may be no host Ollama at all on a Linux server).
        # ...but only if there is RAM for it. Both Ollama models stay
        # resident, so proxying vision to Ollama on a machine that cannot
        # hold both means every scene question evicts the chat model.
        local caption_default="moondream"
        if [[ "$llm_where" == "host" && "$SUGGEST_CAPTION_ADAPTER" == "ollamavlm" ]]; then
            caption_default="ollamavlm"
        fi
        configure_value CAPTION_ADAPTER "Scene-description model" "$caption_default" \
            "Describes what a camera sees. ollamavlm proxies to your Ollama (GPU-fast when the LLM runs on this machine — the default in that case; needs an adapter tag newer than 0.1.3); moondream/blip run inside Docker (moondream answers questions, blip writes plain captions)." "yes" \
            "moondream | blip | ollamavlm — all local."
        # ollamavlm chosen → suggest a VLM sized like the LLM was.
        if [[ "$(env_get CAPTION_ADAPTER)" == "ollamavlm" ]]; then
            explain "Multimodal Ollama model the ollamavlm adapter uses for scene questions; the adapter auto-pulls it. gemma3:4b (tested — clearly better answers) is suggested where RAM allows; moondream is the tested low-RAM pick." \
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
        COMPOSE_ARGS+=(-f "$EXAMPLE_COMPOSE")
        # External-LLM overlay, same condition as compose_args in start.sh.
        # Without it the bundled ollama/ollama-model-pull services are still
        # in an ACTIVE profile here, so `pull` downloads the 3.7 GB
        # ollama/ollama image for containers the launcher then never starts.
        if [[ -n "$(env_get OLLAMA_EXTERNAL_URL)" && "$EXAMPLE_COMPOSE" == *camera-agent.yml ]]; then
            COMPOSE_ARGS+=(-f docker-compose.camera-agent.external-llm.yml)
        fi
        COMPOSE_ARGS+=(--profile "$EXAMPLE_PROFILE")
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
    # Last word before exec — after this, compose owns the terminal and a
    # notice printed 200 lines ago is gone.
    if [[ "${NVIDIA_REBOOT_PENDING:-}" == "yes" ]]; then
        nvidia_reboot_notice
    fi
    exec "$PROJECT_ROOT/start.sh" up
}
main "$@"