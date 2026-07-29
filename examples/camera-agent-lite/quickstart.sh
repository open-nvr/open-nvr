#!/usr/bin/env bash
# ============================================================
# OpenNVR camera-agent-lite — one-command quickstart
# ============================================================
# The camera-agent pattern on a lighter stack: llama.cpp + whisper.cpp +
# Piper + SmolVLM adapter containers (no Ollama), one small agent app with
# chat AND browser-mic voice on the same demo page.
#
#   examples/camera-agent-lite/quickstart.sh          # start
#   examples/camera-agent-lite/quickstart.sh --down   # stop
#
# Adapter images are pulled from GHCR (ghcr.io/open-nvr/*-adapter). Each
# adapter downloads its model from HuggingFace on FIRST boot into a
# persisted volume (~4.3 GB total, reused forever after) — same posture as
# camera-agent's whisper adapter / Ollama model pull. If an image isn't
# published yet, Compose falls back to building it from the sibling
# ai-adapter checkout (../ai-adapter).
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
say() { printf "${CYAN}▸${NC} %s\n" "$1"; }
ok()  { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn(){ printf "${YELLOW}!${NC} %s\n" "$1"; }

# Resolve the repo root from this script's location so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"

ACTION="up"
for arg in "$@"; do
  case "$arg" in
    --down|--stop) ACTION="down";;
    -h|--help)
      cat <<'EOF'
OpenNVR camera-agent-lite — one-command quickstart (run from repo root)

  examples/camera-agent-lite/quickstart.sh          start (chat + voice demo)
  examples/camera-agent-lite/quickstart.sh --down   stop the agent

Adapter images pull from GHCR; models download on first boot into volumes.
Building images locally (pull fallback) needs ../ai-adapter checked out.
EOF
      exit 0;;
    *) warn "ignoring unknown arg: $arg";;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  warn "Docker is required but was not found on PATH. Install Docker Desktop / Engine first."
  exit 1
fi

if [ ! -d ../ai-adapter/adapters/llamacpp ]; then
  warn "ai-adapter repo not found next to open-nvr (../ai-adapter)."
  warn "That's fine when the GHCR images are published (they pull), but the"
  warn "local build fallback won't be available if a pull fails."
fi

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.camera-agent-lite.yml)

if [ "$ACTION" = "down" ]; then
  say "Stopping camera-agent-lite…"
  "${COMPOSE[@]}" --profile camera-agent-lite down
  ok "Stopped."
  exit 0
fi

# 1) Ensure a .env exists (secrets). Prefer the repo's generator; fall back to
#    the example file so a fresh clone still comes up for a local demo.
if [ ! -f .env ]; then
  if [ -x ./scripts/generate-secrets.sh ]; then
    say "No .env found — generating fresh secrets…"
    ./scripts/generate-secrets.sh --write
  else
    say "No .env found — seeding from .env.example (dev secrets; change before exposing)…"
    cp .env.example .env
  fi
  ok ".env ready."
else
  ok ".env already present."
fi

# 2) Bring the stack up. Adapter images pull from GHCR (or build from
#    ../ai-adapter as a fallback); each adapter downloads its model into its
#    weights volume on first boot, then the agent starts.
say "Starting camera-agent-lite (llama.cpp / whisper.cpp / Piper / SmolVLM)…"
say "Profile: camera-agent-lite (Ctrl-C is safe; containers run detached)"
"${COMPOSE[@]}" --profile camera-agent-lite up -d

echo
ok "camera-agent-lite is starting."
printf "  Open ${GREEN}http://localhost:9101/demo${NC} and type — or tap the mic and speak.\n"
echo
say "First boot: the adapters download ~4.3 GB of models into their volumes"
say "and warm up — give it a few minutes. Later boots skip the download."
say "Logs:  ${COMPOSE[*]} --profile camera-agent-lite logs -f"
say "Stop:  examples/camera-agent-lite/quickstart.sh --down"
