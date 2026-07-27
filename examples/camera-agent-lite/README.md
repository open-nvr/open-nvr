# OpenNVR Camera Agent **Lite** — example app

**Ask your cameras, on a shoestring.** The same "agent grounded in live
camera feeds via tool calling" pattern as the
[camera-agent example](../camera-agent/), rebuilt on a deliberately lighter
stack:

| | camera-agent | camera-agent-lite (this) |
|---|---|---|
| LLM runtime | Ollama (`qwen2.5:1.5b`) | **llama.cpp** (Qwen2.5-3B-Instruct GGUF) |
| STT | faster-whisper adapter | **whisper.cpp** adapter (`base.en`) |
| TTS | Piper adapter | Piper adapter (same) |
| Vision | YOLOv8 / BLIP / InsightFace via KAI-C | **SmolVLM2-2.2B** (llama.cpp `--mmproj`) |
| Voice | Pipecat pipeline at `/ws` + HTTP `/converse` | Same Pipecat pipeline at `/ws` + HTTP `/voice` fallback |
| Adapter containers | torch-based (YOLO/BLIP/InsightFace/faster-whisper) | all llama.cpp-family + onnx, **torch-free** |
| Extra services | KAI-C proxy, NATS events | none — agent talks to the 4 adapters directly |

(Pipecat's Silero VAD runs on onnxruntime — no torch — so keeping it costs
the agent image ~70 MB, not gigabytes. The real weight difference is in the
adapter containers.)

Everything still runs locally on CPU — no cloud round-trip, no GPU. One demo
page does both **chat** (type, read) and **voice** (tap the mic and just
talk — Silero VAD detects your turns, replies stream back as speech).

### Run it

From the repo root:

```bash
examples/camera-agent-lite/quickstart.sh          # start (open http://localhost:9101/demo)
examples/camera-agent-lite/quickstart.sh --down   # stop
```

The four adapter images are pulled from GHCR
(`ghcr.io/open-nvr/{llamacpp,whispercpp,pipertts,smolvlm}-adapter`,
published by ai-adapter's `publish-images` workflow). **Models are not baked
into the images**: each adapter downloads its own weights from HuggingFace on
first boot into a persisted Docker volume (~4.3 GB across the four) and
reuses them forever after — the same runtime-download posture as
camera-agent's whisper adapter and Ollama model pull. Give the first boot a
few minutes; later boots skip the download. Offline installs pre-populate
the volumes (or set the adapter's `OPENNVR_*_MODEL_URL=""` to forbid any
fetch).

### No `.env`, no login, no tokens

This example ships **no `.env` and needs no OpenNVR user account**. Camera
discovery and frames come from OpenNVR's internal camera-agent endpoint
(`GET /api/v1/internal/camera-agent/cameras`, authenticated with the stack's
`INTERNAL_API_KEY`), which returns per-camera MediaMTX tap URLs with a signed
token already embedded. The compose overlay injects that key from the
repo-root `.env` into a generated `config.yml` — the same flow the
camera-agent example uses. Frames are grabbed from the tap URL with a
bounded one-shot `ffmpeg` call; the agent never stores camera credentials.

## What it does

```
┌───────────────┐  /ws (streaming voice: raw 16 kHz PCM both ways)
│  Browser tab  │  /ask (typed chat) · /voice (HTTP voice fallback)
│  /demo page   │ ────────────────────────────────┐
└───────────────┘                                 ▼
                                  ┌────────────────────────────────────┐
                                  │ FastAPI agent (camera_agent.py)    │
                                  │  /ws: Pipecat pipeline per session │
                                  │   Silero VAD → whispercpp STT →    │
                                  │   router fast-path / llamacpp LLM  │
                                  │   (5 tools) → pipertts, sentence-  │
                                  │   streamed                         │
                                  └───────────────┬────────────────────┘
                                                  │ look_at_camera →
                                  ┌───────────────┴────────────────────┐
                                  │ OpenNVR roster (internal API key)  │
                                  │  → MediaMTX tap URL → ffmpeg frame │
                                  │  → smolvlm adapter (vision answer) │
                                  └────────────────────────────────────┘
```

The text model never sees pixels. To answer a visual question it calls the
`look_at_camera` tool, which fetches one frame and runs the VLM — and
clearly-visual questions ("what's happening on camera two?") skip the LLM
entirely via a deterministic router, which is both faster and more reliable
than trusting a 3B model to pick the right tool.

The agent is **strictly conversational**: five tools
(`look_at_camera`, `list_cameras`, `get_camera_status`, `current_time`,
`system_status`), no memory, no web access. Recording in OpenNVR is always
on (24/7, mandatory), so there are deliberately **no recording-control
tools** — ask it to stop recording and it will tell you it can't.

## Try these

* "What's happening on camera one?"
* "Is anyone at the door?"
* "Is camera two online?"
* "List cameras"
* "What time is it?"

## Honesty up front

* **No barge-in yet.** The streaming voice path (`/ws`, Pipecat + Silero
  VAD) always finishes its answer before listening again —
  `allow_interruptions` is off because the raw-PCM demo client can't send
  proper cancel frames, so echo/noise would cancel in-flight replies
  (camera-agent ships the same setting for the same reason). The demo also
  gates the mic while the reply plays.
* **Streamed turns are audio-only.** The raw-PCM wire protocol carries no
  text frames, so live-voice turns don't render transcripts in the chat log
  (type to see text, or use the HTTP `/voice` fallback which does return
  the transcript). Same limitation as camera-agent's `/ws`; a production UI
  would use `@pipecat-ai/client-js` + the protobuf serializer.
* **Cameras must be enrolled in OpenNVR.** There is no webcam / bring-your-own
  camera path here (the full camera-agent has one). For camera-less tests, a
  static `cameras:` list with `file://` or `http(s)://` frame URLs works —
  see `config.example.yml`.
* **Temporal questions are best-effort.** "Did someone walk past?" samples the
  live view three times over ~2 s; there is no historical-frame API.
* **Building images locally needs `ai-adapter` as a sibling checkout.** The
  overlay is pull-first (GHCR), but if an image hasn't been published yet
  Compose falls back to `build:` from `../ai-adapter`.

## Configure

The quickstart generates its config from [config.docker.yml](config.docker.yml).
For running the agent directly on the host (development), copy
[config.example.yml](config.example.yml) to `config.yml` and:

```bash
cd examples/camera-agent-lite
uv sync
uv run python camera_agent.py --config config.yml
```

Key knobs:

| Field | Default | Effect |
|---|---|---|
| `adapter_token` | — | Bearer token for the four adapters (= `INTERNAL_API_KEY`; empty only if adapters run in dev mode). |
| `llm_url` / `stt_url` / `tts_url` / `vlm_url` | `127.0.0.1:9014/9013/9012/9016` | Where the llamacpp / whispercpp / pipertts / smolvlm adapters live. |
| `opennvr_cameras_url` + `opennvr_api_key` | — | Auto-discover cameras from OpenNVR (internal endpoint, no user login). |
| `cameras` | `[]` | Static roster instead: `{camera_id, name, role, frame_url}` with `file://`, `http(s)://`, or `rtsp://` URLs. |
| `frame_cache_ttl_seconds` | `2.0` | Reuse one camera's frame across tool calls in a single turn. |
| `llm_max_tokens` | `200` | Keep spoken answers short. |
| `routing_min_confidence` | `0.6` | Below this the deterministic router defers to the LLM. |

## Tests

```bash
cd examples/camera-agent-lite
uv sync --extra dev
uv run pytest -q      # 59 tests, no models/adapters/hardware needed (mocks)
```

Tests cover the router (vision vs command vs text, camera-reference
parsing), tool schema validation and unknown-tool rejection, the tool
handlers against a static file camera, the camera roster (internal-key auth,
caching, error surfaces), frame-source scheme dispatch and credential
redaction, config loading, and the brain's three answer paths (vision
fast-path, LLM tool loop, deterministic degradation when the LLM is down).
