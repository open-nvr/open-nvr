# OpenNVR Agent (formerly Camera Agent) — example app

> **Naming note:** "OpenNVR Agent" is the official product name; only
> user-facing strings were renamed. The directory
> (`examples/camera-agent/`), compose service names, Python modules,
> config keys, and compose profile names deliberately keep the
> `camera-agent` naming for infra compatibility.

**Ask your cameras.** An agent that grounds its answers in live camera
feeds via tool calling (YOLOv8 / InsightFace / BLIP) — running on CPU,
on your homelab, no cloud round-trip. Run it two ways, same app:

- **Voice** (default) — tap **Talk** and speak; it listens continuously and
  replies through Piper TTS, with a named persona and avatar. The session stays
  live for follow-ups until you tap **Stop** or a quiet spell auto-stops it.
- **Chat** (`--chat`) — type your question, read the answer. Same tools
  and scene description, no microphone/speaker, so it's lighter.

The brain runs locally (Ollama) by default, or point it at any
OpenAI-compatible endpoint — bring your own (see `config.cloud.yml`).

This is the agent example for OpenNVR v0.1. It demonstrates the
pattern of "OpenNVR camera as participant", not just camera as data
source.

**Where this is headed:** today the agent lives on its own demo page.
The next milestone extends the *same* agent to join **LiveKit rooms**
(and similar real-time/voice surfaces) as a participant — so you can
reach it from a phone, a meeting, or a kiosk, not just the local web
UI. One sovereign agent, widely available, with no change to the tools
or the local-first brain.

### Run it

From the repo root:

```bash
examples/camera-agent/quickstart.sh          # voice  (open https://localhost:9100/demo, tap Talk, speak)
examples/camera-agent/quickstart.sh --chat   # chat   (type your question instead)
examples/camera-agent/quickstart.sh --down   # stop
```

First boot pulls the small LLM (default `qwen2.5:1.5b`) and warms the adapters.
On a low-RAM box: `OLLAMA_MODEL=qwen2.5:0.5b examples/camera-agent/quickstart.sh`.

### Read in detail

- [**Models & latency**](MODELS_AND_LATENCY.md) — how model choices were weighed
  for CPU latency and good UX, plus the efficient model picks.
- [**Alarms**](ALARMS.md) — ringing alarms, time windows, presets, and the
  documented emergency-calling hook.
- [**Notifications**](NOTIFICATIONS.md) — external webhook/push delivery.
- [**Faces & watchlist**](FACES.md) — enrollment and watchlist matching.

## What it does

```
┌───────────────┐    speech (WS, 16k mono PCM)
│  Browser tab  │ ──────────────────────────────┐
│  /demo page   │                               │
└───────────────┘                               ▼
                              ┌───────────────────────────────────┐
                              │ FastAPI /ws + Pipecat transport   │
                              └──────────────┬────────────────────┘
                                             │ audio frames
                                             ▼
                              ┌───────────────────────────────────┐
                              │ Silero VAD (turn detection)       │
                              └──────────────┬────────────────────┘
                                             │ utterance bytes
                                             ▼
                              ┌───────────────────────────────────┐
                              │ Whisper adapter (STT)             │
                              └──────────────┬────────────────────┘
                                             │ "what's at the porch?"
                                             ▼
                              ┌───────────────────────────────────┐
                              │ Ollama adapter (qwen2.5:1.5b)     │
                              │   5 registered tools              │
                              └──────────────┬────────────────────┘
                                             │ tool calls →
              ┌──────────────────────────────┼─────────────────────┐
              │                              │                     │
              ▼                              ▼                     ▼
    ┌────────────────┐  ┌────────────────────┐  ┌──────────────────┐
    │ KAI-C → BLIP   │  │ KAI-C → YOLOv8     │  │ KAI-C →          │
    │ scene caption  │  │ object detection   │  │ InsightFace      │
    └────────────────┘  └────────────────────┘  └──────────────────┘
                                             │
                                             │ final assistant text
                                             ▼
                              ┌───────────────────────────────────┐
                              │ Piper adapter (TTS)               │
                              └──────────────┬────────────────────┘
                                             │ PCM audio
                                             ▼
                              ┌───────────────────────────────────┐
                              │ FastAPI /ws transport → browser   │
                              └───────────────────────────────────┘
```

Single correlation_id threads through every step so KAI-C's audit
log shows: agent question → tool calls → final answer.

## Why this matters

OpenNVR already does "watch and notify". This example flips the
posture — the cameras have a voice. You ask a question, the agent
runs only the tools needed to answer (one frame from one camera,
not all of them), and tells you. It's deliberately a SMALL agent,
not "AGI for cameras":

* The model has five tools, no general-purpose memory, no web
  access. It can describe what it sees, count objects, recognise
  faces, look back at recent inference events, and — when a
  footage-search index is configured (`footage_index_path`) — search
  the recorded past in natural language ("did a red truck come by the
  dock earlier?") via the `search_footage` tool.
* It can't drive cameras (pan-tilt-zoom), can't arm / disarm
  anything, can't speak first. Strictly conversational.
* Latency is "homelab-fine, not real-time" — expect 3-6 seconds
  per round-trip on CPU. Not Alexa-snappy. Defensible for
  "ask your cameras" but not for streaming dialogue.

## Honesty up front

Real-world limitations the example does NOT yet handle:

* **Demo is voice-only — no on-screen transcript.** The bundled
  `/demo` HTML uses a raw-PCM WebSocket protocol (see
  `serializer.py`) and the `RawPcmSerializer` drops every non-audio
  frame on the wire. You'll *hear* the agent speak its answer but
  the demo page won't surface what you said or what the agent
  replied as text. A production UI bundles
  `@pipecat-ai/client-js` plus the matching Pipecat
  `ProtobufFrameSerializer` on the server and gets transcripts,
  control frames, and metrics on the same WebSocket.
* **Audit-chain split.** KAI-C v0.1 only proxies application/json
  inference calls. The streaming voice path (Whisper, Ollama,
  Piper) therefore calls the adapters DIRECTLY — bypassing the
  central KAI-C audit log. Each adapter still records the call
  in its own audit log, so nothing is invisible, but you won't
  see the voice path in KAI-C's central history until v0.2. The
  tool calls into BLIP / YOLOv8 / InsightFace DO go through
  KAI-C and are fully audited.
* **Model quality.** `qwen2.5:1.5b` (the default) is small, fast on
  CPU, Apache-2.0, and non-thinking — the sweet spot for tool-calling
  on a weak box. Drop to `qwen2.5:0.5b` on very low RAM, or bump to
  `qwen2.5:3b` for better grounding at more RAM and slower inference.
  Set it via `OLLAMA_MODEL` in `.env` (or `llm_model` in your config).
* **No memory across sessions.** Each new WebSocket connection
  starts fresh. "What did you tell me yesterday?" won't work.
  Use the `recent_events` tool with a long window for ad-hoc
  recall against NATS history.
* **No real interrupts.** Pipecat's barge-in support is set up
  (`allow_interruptions=True`), but the demo HTML client doesn't
  yet send the right cancel frames when you start talking again.
  Wait for the agent to finish before asking the next thing for
  v0.1.
* **Browser demo is minimal.** ~200 lines of vanilla JS. Audio
  worklets, jitter buffering, transcript display — none of it.
  The intent is to demonstrate the agent shape; production UIs
  should use `@pipecat-ai/client-js`.

## Quick start

**Fastest path:** from the repo root, `examples/camera-agent/quickstart.sh`
(add `--chat` for the lighter text version, `--down` to stop). You can click
"Use this machine's camera" in the demo to run against your laptop webcam. The
manual, adapter-by-adapter setup below is for development/debugging.

```bash
# 1. Start the adapters you need (in the ai-adapter repo). The
#    agent uses six adapters total: Whisper, Ollama, Piper for
#    the voice path; BLIP, YOLOv8, InsightFace for the tool path.
#    The bundled ai-adapter docker-compose covers them all.
cd ai-adapter
docker compose up -d whisper ollama piper blip yolov8 insightface

# 2. Pull a tool-capable LLM into Ollama (one-time, ~1GB).
docker exec ai-adapter-ollama-1 ollama pull qwen2.5:1.5b

# 3. Start KAI-C and register the vision adapters. Ports come from
#    the docker-compose service definitions in ai-adapter — adjust
#    if you remapped them.
cd ../open-nvr/kai-c
INTERNAL_API_KEY=$(openssl rand -hex 32)
AI_SOVEREIGNTY=local_only INTERNAL_API_KEY=$INTERNAL_API_KEY \
  python -m uvicorn main:app --host 0.0.0.0 --port 8100 &

register() {
  local name=$1 url=$2
  curl -X POST http://localhost:8100/api/v1/adapters/register \
    -H "X-Internal-Api-Key: $INTERNAL_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$name\",\"url\":\"$url\"}"
}
register piper       http://127.0.0.1:9001
register yolov8      http://127.0.0.1:9002
register whisper     http://127.0.0.1:9003
register fast-plate-ocr http://127.0.0.1:9004
register insightface http://127.0.0.1:9005
register blip        http://127.0.0.1:9006

# 4. Configure
cd ../examples/camera-agent
cp config.example.yml config.yml
# edit config.yml: kaic_api_key, the three streaming-adapter tokens,
# and the camera_id + frame_url for at least one camera

# 5. Run
python camera_agent.py --config config.yml

# 6. Open https://localhost:9100/demo, click Start, and speak.
```

## Try these

* "What's on the front porch?"
* "Is there a person at the front door right now?"
* "Did anyone come to the porch in the last ten minutes?"
* "Who's at the back door?"
* "How many cars are in the driveway?"

## Configure

See `config.example.yml` for the full set. Key knobs:

| Field | Default | Effect |
|---|---|---|
| `llm_model` | `qwen2.5:1.5b` | Tool-capable Ollama model (Apache-2.0, non-thinking). `qwen2.5:0.5b` for low RAM, `qwen2.5:3b` for better grounding. |
| `llm_temperature` | `0.4` | Tool calling works best at low-but-not-zero temperatures. |
| `frame_cache_ttl_seconds` | `2.0` | How long to reuse one camera's frame across tool calls in a single LLM turn. |
| `event_ring_size` | `256` | Per-camera ring buffer size for the `recent_events` tool. |
| `nats_inference_url` | unset | Set this to enable `recent_events` against your inference bus. Without it the tool returns "no events". |
| `cameras[].role` | `"(no role configured)"` | One-sentence role description per camera — gets baked into the system prompt so the LLM knows what each camera watches. |
| `opennvr_cameras_url` | unset | When set and `cameras` is empty, fetches the camera roster from a running OpenNVR instance (`GET /api/v1/internal/camera-agent/cameras`). This means you never duplicate RTSP credentials in this file — OpenNVR owns the camera connection and returns MediaMTX tap URLs. |
| `opennvr_api_key` | unset | API key for the `opennvr_cameras_url` endpoint. Must match `INTERNAL_API_KEY` in OpenNVR's `.env`. Falls back to `kaic_api_key` if unset. |
| `avatar_video` | `true` | Play the talking-avatar video clips in the demo. `false` uses the built-in animated SVG face only. |

### Swap in your own avatar (offline)

The demo shows a talking avatar next to the conversation. It's a plain HTML
`<video>` that plays a looping clip per state, so you can replace it with a
human presenter without touching any code — just drop your files in, keeping
the names:

```
demo/avatar/idle.webm       demo/avatar/idle.mp4        # calm loop, small movements
demo/avatar/speaking.webm   demo/avatar/speaking.mp4    # talking loop (plays during TTS)
demo/avatar/thinking.webm   demo/avatar/thinking.mp4    # optional — plays while it computes
```

Provide both `.webm` (VP9, preferred) and `.mp4` (H.264, Safari fallback); any
square resolution works (the bundled placeholders are 256×256). The UI switches
to `speaking` during playback and back to `idle` after; `thinking` is optional
and falls back to the idle clip if absent. If a clip fails to load — or you set
`avatar_video: false` — it degrades to the animated SVG face, so the demo never
breaks.

**Stay offline — no cloud avatar services.** At runtime the demo never contacts
anything: it only plays local video files, so playback is already sovereign. The
one thing to watch is *how the clips are produced*. Generate them with a tool
that runs on your own machine so nothing (your script, your likeness, the reply
audio) ever leaves it:

- **Local pre-render** — [SadTalker](https://github.com/OpenTalker/SadTalker)
  (one portrait + audio → a talking-head clip) or
  [Wav2Lip](https://github.com/Rudrabha/Wav2Lip) (a base video + audio →
  lip-synced clip), both run locally. Render an `idle`/`speaking`/`thinking`
  loop once and drop them in.
- **Avoid cloud generators** (HeyGen and similar) — even a one-time render
  uploads your script/likeness to their servers, which breaks end-to-end
  sovereignty. Only use one if you're comfortable with that trade-off for a
  throwaway concept demo.
- **Zero-dependency default** — the built-in SVG face is fully local and its
  mouth is already driven by the live TTS amplitude, so it "speaks" in real time
  with no assets and no network at all.

Real-time *local* lip-sync (driving a face model from the TTS stream on-device)
is the natural next step for a more human, fully-sovereign avatar — a good
follow-up once a local renderer is chosen.

## Tests

```
cd examples/camera-agent
uv sync --extra dev
uv run pytest -q
```

Tests cover:

* Config loader (required fields, numeric coercion, role defaults,
  per-camera roster in system prompt)
* `CameraContext` frame cache (TTL, concurrent fetches, invalidation,
  unknown-camera + missing-source errors, FrameSourceError propagation)
* Event ring (window filter, per-camera filter, ordering, bounded
  size, all-cameras wildcard)
* NATS event parser (subject parsing, fallback for unknown payloads,
  face/object/scene summary phrasing)
* Tool handlers (describe / detect / recognise / recent_events:
  happy path, unknown camera, empty result, adapter exception,
  arg validation, label-count cap)
* Tool definitions (camera enum baked into schema, `__any__`
  wildcard for `recent_events`, sentinel-when-empty)

The Pipecat pipeline assembly itself (`services.py` and
`build_pipeline_task`) is exercised by running the daemon and
talking to it — no unit tests against Pipecat's internals since
those APIs are still maturing.
