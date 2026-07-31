#!/usr/bin/env bash
# ============================================================
# OpenNVR Agent (formerly Camera Agent) — one-command quickstart
# ============================================================
# Clone → run → talk to your cameras. Two ways to run the SAME agent:
#
#   examples/camera-agent/quickstart.sh          # VOICE (default): speak, hear answers
#   examples/camera-agent/quickstart.sh --chat   # CHAT: type, read answers (lighter)
#   examples/camera-agent/quickstart.sh --down    # stop
#
# Voice = Whisper STT + Ollama LLM + Piper TTS + YOLOv8/BLIP vision.
# Chat  = the same tools and scene description, no microphone/speaker.
#
# The Ollama model (default qwen2.5:1.5b) is pulled automatically on first start.
# Low-RAM box? Override it:  OLLAMA_MODEL=qwen2.5:0.5b examples/camera-agent/quickstart.sh
# Cloud / bring-your-own brain? See examples/camera-agent/config.cloud.yml.
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

PROFILE="camera-agent"; MODE="voice (speak / hear)"
ACTION="up"
for arg in "$@"; do
  case "$arg" in
    --chat|--text) PROFILE="camera-agent-chat"; MODE="chat (type / read)";;
    --voice|--full) PROFILE="camera-agent"; MODE="voice (speak / hear)";;
    --down|--stop) ACTION="down";;
    -h|--help)
      cat <<'EOF'
OpenNVR Agent (formerly Camera Agent) — one-command quickstart (run from repo root)

  examples/camera-agent/quickstart.sh          voice (default): speak, hear answers
  examples/camera-agent/quickstart.sh --chat   chat: type, read answers (lighter)
  examples/camera-agent/quickstart.sh --down   stop the agent

Override the LLM:  OLLAMA_MODEL=qwen2.5:0.5b examples/camera-agent/quickstart.sh
Cloud / BYO brain: see examples/camera-agent/config.cloud.yml
EOF
      exit 0;;
    *) warn "ignoring unknown arg: $arg";;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  warn "Docker is required but was not found on PATH. Install Docker Desktop / Engine first."
  exit 1
fi

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.camera-agent.yml)

if [ "$ACTION" = "down" ]; then
  say "Stopping the camera agent…"
  "${COMPOSE[@]}" --profile camera-agent --profile camera-agent-chat down
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

# 2) Bring up the chosen mode. First boot auto-pulls the small LLM and warms
#    the adapters, then starts the agent.
say "Starting the camera agent — ${MODE}"
say "Profile: ${PROFILE} (Ctrl-C is safe; containers run detached)"
"${COMPOSE[@]}" --profile "$PROFILE" up -d

echo
ok "Camera agent is starting."
printf "  Open ${GREEN}https://localhost:9100/demo${NC} and "
if [ "$PROFILE" = "camera-agent" ]; then
  printf "click Start, then speak.\n"
else
  printf "TYPE a question, e.g. \"how many people are at the door?\"\n"
fi
echo
say "Also reachable from other LAN devices at https://<this-host-ip>:9100/demo."
say "First visit: accept the one-time self-signed-certificate warning, then"
say "sign in with your OpenNVR account (device-firewall approval applies too)."
say "First boot pulls the model and warms up — give it a minute."
say "Logs:  ${COMPOSE[*]} --profile ${PROFILE} logs -f"
say "Stop:  examples/camera-agent/quickstart.sh --down"
