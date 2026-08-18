# Models & latency — choosing for good UX

This agent does **not** build or train any models. It orchestrates pre-built
adapters (Whisper, Ollama, Piper, YOLOv8, BLIP, InsightFace). Which models you
point it at, and how hard you poll them, decides whether the experience feels
snappy or sluggish. This page records the deliberate choices and the knobs.

## The latency budget (one spoken turn)

```
mic → ffmpeg transcode → Whisper STT → LLM (tool-calling) → vision detect →
LLM (compose) → Piper TTS → playback
```

On CPU, the LLM and STT dominate. Targets that feel acceptable:
- First turn: a few seconds (cold model load is pre-warmed away — see below).
- Warm turns: ~2–5 s end to end.

Anything that adds a synchronous step (an extra tool call, a slow model) is
felt directly, so the model picks below favour *fast and good-enough* over
*slow and perfect*.

## Running on limited hardware (no GPU, little RAM)

A real CPU-only test (Win 11, 11.5 GiB) gave the numbers that drive this advice:

- **The LLM is *not* CPU-intensive — it's memory-bandwidth-bound.** Ollama showed
  only ~5–25% CPU while generating; it's slow because it streams the model's
  weights through CPU caches each token. The practical consequence: **a smaller
  model is both lighter *and* faster** on weak hardware. No GPU is required —
  a GPU just makes it ~10–20× faster.
- **The CPU hog is actually Piper TTS** (~390%, nearly 4 cores) — so **text mode
  avoids the most CPU-intensive stage entirely.**
- **RAM is modest** (~3.4 GiB peak for the full stack); compute is the limit.

So, in order, for limited hardware:

1. **Run chat instead of voice:** `examples/camera-agent/quickstart.sh --chat`
   skips the CPU-heavy Piper TTS (and Whisper) entirely — the single biggest
   CPU win on a weak box.
2. **Use a smaller LLM:** `OLLAMA_MODEL=qwen2.5:0.5b examples/camera-agent/quickstart.sh --chat`
   (~0.5 GB, the smallest model that still tool-calls reliably). Default is
   `qwen2.5:1.5b` (~1 GB); below `0.5b` grounding gets unreliable, so that's the floor.
3. **Cap CPU + context in config** so the LLM doesn't peg the box:
   ```yaml
   llm_num_threads: 2     # leave cores for the rest of the machine
   llm_num_ctx: 2048      # smaller window = less RAM + faster prefill
   ```
   and in `.env`, `OLLAMA_KEEP_ALIVE=5m` frees the model's RAM when idle (at the
   cost of a cold reload on the next turn). The compose already sets
   `OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_NUM_PARALLEL=1`.
4. **Or offload the brain to the cloud** (lightest *local* footprint): point the
   agent at any OpenAI-compatible endpoint (`config.cloud.yml`) — vision stays
   local, the LLM runs on your key — ~0 local LLM RAM/CPU, ~1 s replies, no
   hallucination. Not sovereign — an explicit opt-in.

Rule of thumb: **chat + small local model = lightest fully-local; cloud LLM =
lightest on the local machine.**

### Disk / image size (it's not "for CPU")

Two images look huge but the size isn't a CPU requirement:

- **`ollama/ollama` ~10 GB** — the official image **bundles NVIDIA CUDA + AMD
  ROCm GPU libraries** so it *can* use a GPU. On a CPU box that's ~6–7 GB of
  unused libs. If disk is tight, point the agent at any OpenAI-compatible LLM
  endpoint instead (e.g. a llama.cpp server, a few hundred MB) via
  `config.cloud.yml` — the LLM client doesn't care what's behind the URL.
- **`blip-adapter` ~5.5 GB** — PyTorch + transformers + baked weights (the torch
  CPU wheel alone is ~2.5–3 GB). It's the heaviest single piece of the stack;
  the `--chat` path still uses it for scene description.

Neither size is a *CPU* requirement — both are disk, and the LLM image shrinks if
you swap the backing service. The defaults favour a one-command local run.

### Measuring it
Every `/converse` turn returns a `timings_ms` breakdown
(`transcode`/`stt`/`llm`/`tts`/`total`), and the text `/ask` turn returns
`latency_ms`. The demo UI shows a per-turn chip (`STT 420 · LLM 1200 · TTS 300
· 1.9s`). For a repeatable load test against a live agent, run the harness:

```bash
python tools/latency_harness.py --url http://localhost:9100 \
    --audio question.wav --turns 8 --load-monitors 6
```

It reports p50/p95 per phase and quantifies how much background polling
(watches/alarms) steals from the live turn.

## Turn detection & background-noise rejection

A hands-free loop only feels good if it (a) ends the turn when you stop talking
and (b) doesn't react to room noise. Two layers handle this:

- **Client VAD (browser).** The mic uses `echoCancellation` + `noiseSuppression`
  + `autoGainControl`, then an RMS energy gate that **adapts to the ambient
  noise floor** (a rolling EMA of quiet frames): the start gate is
  `max(floor, noiseFloor × 2.2)`, so steady background noise never crosses it,
  while soft speech still does. Endpointing stops the turn after ~900 ms of
  silence (min 350 ms, max 15 s), and capture is suppressed while the agent is
  speaking so it never hears itself.
- **Server STT guard (`stt_noise_filter`, on by default).** Whisper hallucinates
  stock phrases from silence/noise — "Thank you.", "you", "Thanks for watching".
  `looks_like_noise()` drops these so a noisy room can't trigger a phantom turn;
  the UI just keeps listening. Set `stt_noise_filter: false` to disable.

## Recommended model choices

| Role | Default (snappy) | Upgrade (quality, slower) | Why |
|------|------------------|---------------------------|-----|
| LLM | `qwen2.5:1.5b` (non-thinking) | `qwen2.5:3b` / `llama3.1:8b-instruct` | Must support tool-calling. 1.5B answers tool calls in ~1–2 s warm on CPU; bigger is slower. Qwen2.5 dense models are Apache-2.0. `qwen2.5:0.5b` is the low-RAM floor. |
| STT | faster-whisper `base.en` | `small.en` | `.en` is English-only — faster and far fewer hallucinated tokens on quiet audio than multilingual. |
| TTS | Piper `en_US-libritts-high` | (voice of choice) | Piper is fast and CPU-friendly; pick the voice to match the persona gender. |
| Detect | YOLOv8n (`yolov8n.onnx`) | YOLOv8s/m | n is the fastest; larger nets cost latency per frame and per poll. |
| Caption | BLIP | — | Optional; `describe_camera` falls back to the detector if absent. |
| Faces | InsightFace | — | Optional; only loaded for recognition/enrollment. |

Keep the LLM **warm**: `OLLAMA_KEEP_ALIVE=-1` plus the startup pre-warm (model +
system/tools prompt prefix) means even the first real question skips the cold
load. The vision detector is pre-warmed too.

## Where latency hides — and the knobs

The biggest risk to UX isn't a single turn; it's **background polling stealing
the detector** from the live conversation:

- Each watch (`monitor`), alarm, and crossing counter polls every camera it
  covers on its interval and runs a detect. `kind=all` × N alarms × short
  intervals can saturate a CPU detector and make the chat path stall.
- Defaults are deliberately conservative: monitors poll every **8 s**, alarms
  every **5 s**, the report scheduler ticks every **30 s**, and fetched frames
  are cached for **2 s** so multiple tools in one turn hit the camera once.

Tuning guidance:
- Prefer specific cameras over `all` for always-on alarms/watches.
- Raise the poll intervals if you add many watches/alarms (they're set in
  `MonitorManager` / `AlarmManager` constructors).
- On a busy box, give the vision adapter a GPU (`USE_GPU=true` in the adapter
  build) — detection latency drops an order of magnitude and polling stops
  competing with the live turn.
- Crossing counters request tracking (`task=track`), which is heavier than a
  plain detect and needs a higher frame rate to be accurate — use sparingly.

## Honest limits
- Snapshot counts and crossing counts are only as good as the poll rate and the
  tracker; they are not certified people-counting.
- Detection is limited to the model's classes (COCO YOLOv8 has no "fire" — the
  Fire alarm preset needs a fire/smoke model registered).


## Running the LLM outside Docker (macOS / Windows: do this)

Containers on macOS and Windows run inside a VM with **no GPU access** —
the bundled ollama container answers on plain CPU, and one agent turn can
take minutes (the demo's latency chip will show `LLM` dominating the
turn). Point the agent at an LLM runtime **on the host** instead:

1. Install [Ollama](https://ollama.com) on the machine and pull your
   model: `ollama pull qwen2.5:1.5b`
2. Set in `.env`: `OLLAMA_EXTERNAL_URL=http://host.docker.internal:11434`
3. `./start.sh up` — it adds the
   `docker-compose.camera-agent.external-llm.yml` overlay automatically,
   which skips the bundled ollama container (and its 3.2 GB image).

On Apple Silicon this moves inference onto the Metal GPU: the same turn
that took minutes lands in seconds. Any Ollama-compatible endpoint works,
including one on another LAN machine. The installer offers this as the
"LLM runtime" question when you select the camera-agent example.
