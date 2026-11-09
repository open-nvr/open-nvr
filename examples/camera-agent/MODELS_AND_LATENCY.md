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

## Four rules for a fast turn on CPU

A field trace on an 8-core CPU box read
`stt 14s → llm (iter 1) 17s → search_history 12s → llm (iter 2) 2.6s → tts 25s · 71s`.
Most of it was contention (the same box was burning ~3 cores on a
captioner that could not keep up — see the enrichment governor), but
four things in the agent itself made every stage slower than it needs
to be, and each is now a rule:

1. **The prompt's static prefix is byte-identical across turns.** Ollama
   reuses its KV cache only for an unchanged prefix, and its chat template
   renders the tool schemas INSIDE the system turn, right after the system
   text. The clock line (which changes every minute) used to sit in the
   system prompt, ahead of ~3.5k tokens of schemas, so the whole prefix
   was re-prefilled on every turn — the 17 s "iter 1" against 2.6 s for
   "iter 2" of the same turn. On the `/converse` path the clock now rides
   IN the user turn (`[The current date and time is …]` on its own line
   above the user's words); the pre-warm sends the same clockless system
   + tools. Not a system message after the history: Ollama collects
   every system-role message into the one leading system block, which the
   Qwen and Llama templates render before the tool schemas — so a
   "trailing" system clock still busts the prefix, and several
   OpenAI-compatible servers reject a second system turn outright. Keep
   it that way: anything that varies per turn belongs in the user turn.
   (The streaming `/ws` context keeps the clock in its system prompt
   because that context is built once per session and does not change
   within it.)
2. **Whisper runs greedy (`beam_size: 1`).** Beam 5 costs 30-50% more CPU
   and buys nothing on a clean, VAD-trimmed utterance of a few seconds.
3. **The Piper voice is a MEDIUM tier by default** (`PIPER_VOICE`,
   `en_US-lessac-medium`). Piper is the CPU hog of the stack (~390% on a
   4-core box); the tier is the cost, and "high" is 2-3x the compute of
   "medium" for the same intelligibility. Kokoro sounds better still and
   runs ~9x slower than Piper on CPU — the wrong direction here.
4. **`search_history` fetches each kept photo once, in parallel**, and
   face-matches only when the question asks WHO. It used to fetch every
   crop twice, one at a time, and run recognition on every person answer.

The streaming voice path (`/ws`, Pipecat) already synthesises per
sentence, so audio starts after the first sentence; the JSON `/converse`
path returns one clip for the whole reply and is bounded by the rules
above.

## The router: decide before the model does (`router.py`)

The question space is small — what is on a camera NOW, or what CAME BY
in some window; one camera or all; one class of thing; one time phrase
— and every one of those used to go to a 1.5B model with fifteen tool
schemas and a request to compute an ISO window from the clock. The
regexes that answer those questions already existed in the agent
("forced grounding" ran them AFTER the model failed to call a tool).
The router runs them FIRST, when they are sure.

| Tier | When | What happens | LLM cost |
|---|---|---|---|
| **0** | every slot resolves: camera (named, "all", the one the UI is on, or the only one), tool, object, window; one question; no side effect; not about WHO | the tool runs at once; the model is asked only to **say** the answer, with a ~200-token compose prompt and **no tools** | one short call — the tool-calling iteration is skipped entirely |
| **1** | the phrasing resembles one tool's but a slot is missing (two cameras, none named) | the full prompt goes out **unchanged** (the cached prefix holds) with one line in the user turn, next to the clock, naming the likely tool | as today, fewer wrong-tool iterations |
| **2** | everything else — arm/watch/report/stop, "who came", two questions, a story | the loop, exactly as before | as today |
| **0, no tool** | small talk: a greeting, thanks, a closing, "what can you do" | the loop with **no tools**: the model answers in words and cannot sweep the cameras; the anti-fabrication guard still runs, so a reply that claims a sighting anyway gets a real look | one call, no tool schemas |

The trace shows the tier (`route: tier0 detect_objects` → `detect_objects`
→ `llm: compose`), and every turn logs `router: tier=… reason=…`, so the
hit rate is readable from the logs and the exemplar phrasings in
`router.py` can grow from real misses. Both tiers are switches in the
config (`router_tier0`, `router_hints`).

Tier 1 is lexical (content-word overlap with per-tool exemplars,
synonyms folded), not an embedding model: the hint only steers a model
that still sees the whole prompt, and a sentence-embedding model would
add ~150 MB to the agent image for that.

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
   (~0.5 GB, the floor). It answers fastest but leans on the router and the
   grounding guard — see the measured table below for what each size gives up;
   below `0.5b` grounding gets unreliable.
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

Speed is half of picking a model; the other half is whether it calls the
right tool. `tools/eval_harness.py` measures that against the same live agent
(see "Measuring better" in [`AGENT_DESIGN.md`](AGENT_DESIGN.md#measuring-better--the-eval-strategy)):

```bash
python tools/eval_harness.py --url http://localhost:9100 \
    --camera <id> --repeat 5 --throwaway --out qwen2.5-1.5b.json
```

## Turn detection & background-noise rejection

A hands-free loop only feels good if it (a) ends the turn when you stop talking
and (b) doesn't react to room noise.

**Which path you are on matters.** The agent has two voice paths:

* The **demo page's Talk mode** (`/demo`) now streams by default: the
  microphone goes to `/ws` as 16 kHz PCM from an AudioWorklet, and the
  page plays each sentence as it arrives and renders the transcript, the
  reply and the turn state from JSON events the pipeline sends beside the
  audio (`services.StreamEvents`). Its "turn by turn" setting is the old
  path: record an utterance, wait 0.9 s of silence, POST it to
  `/converse`, play the whole answer when every stage is done. Measured
  on the Ventuno Q before the switch: 3–7 s of nothing after you stopped
  talking, no interrupting (the office session of 2026-11-08).
* The **streaming pipeline** (`/ws`, Pipecat 1.12) is where Silero VAD +
  **Smart Turn v3** live: semantic end-of-turn (its silence fallback is
  1.0 s, was 2.0), and model-backed interruptions (`turns.py`: a reply
  is only cut when the interrupter has actually said a few words). Since
  2026-11-08 it answers with the agent's own turn — the router, the forced
  grounding, the window fit, the backup model and the reply guards — so
  the two paths cannot drift apart.

So if the demo page "stopped speaking when someone said a couple of random
words", that was the client barge-in gate (now *firm* by default, see
below), not Smart Turn.

### Interruptions on the streaming pipeline (`turns.py`)

On `/ws` the question "should the agent yield?" is answered the way a
person answers it, from four cues the pipeline already has — and only
while the agent is speaking (when it is silent the first sound of speech
opens the turn, so Smart Turn's end-of-turn detection is untouched):

| cue | source | rule (config) |
|---|---|---|
| it is speech | Silero VAD start/stop pair | a cough or a chair never makes one |
| it lasts | the pair's span | ≥ `interrupt_min_ms` (300) |
| it has words | Whisper transcript, backchannels stripped | ≥ `interrupt_min_words` (2); "yeah", "okay", "mm-hm" count for nothing |
| it is for the agent | `addressed_to_agent()` | its name, a question, a request/correction ("no, wait", "show", "the other"), the site's own words (camera names, gate, plate…) — vs a third-person aside ("he said lunch is at one") or a long narrative with none of those |

A phrase that passes all four interrupts (an `InterruptionFrame`; the
reply is cut and the phrase becomes the next user turn); anything else is
dropped from the aggregation and the agent keeps talking. If the agent
has already gone quiet by the time the transcript lands, the addressee
test is skipped — nobody was being interrupted. Every decision is logged
(`interruption accepted/ignored (<reason>): '<text>'`) and kept in
`GET /interruptions` with the text, duration and reason, which is the
evidence to tune the three knobs from on your own site. `interruptions:
eager` restores plain VAD barge-in; `off` lets the agent always finish.

Cost: the decision needs the transcript, so on CPU it lands ~0.2 s (VAD
stop) + Whisper's time after the interrupter pauses — a *firm* yield, not
an instant one. What it does not do: read gaze, or know voices — a
wake-word mode and per-operator voice enrolment are the two stronger
addressee signals, both possible later on the same gate.

Two layers handle the demo page:

- **Client VAD (browser).** The mic uses `echoCancellation` + `noiseSuppression`
  + `autoGainControl`, then an RMS energy gate that **adapts to the ambient
  noise floor** (a rolling EMA of quiet frames): the start gate is
  `max(floor, noiseFloor × 2.2)`, so steady background noise never crosses it,
  while soft speech still does. Endpointing stops the turn after ~900 ms of
  silence (min 350 ms, max 15 s), and capture is suppressed while the agent is
  speaking so it never hears itself. **Barge-in** (talking over the agent)
  is a separate gate on the same mic: *firm* (default) stops the reply only
  after ~550 ms of sustained speech well above the noise floor (short dips
  tolerated — speech has gaps), so a cough, a word to someone else, or a
  chair scraping does not; *eager* stops on the first clear syllable (the
  old ~70 ms rule); *off* always lets the agent finish. Set it under the
  header ("interrupt when I talk over it"); persisted per browser.
- **Server STT guard (`stt_noise_filter`, on by default).** Whisper hallucinates
  stock phrases from silence/noise — "Thank you.", "you", "Thanks for watching".
  `looks_like_noise()` drops these so a noisy room can't trigger a phantom turn;
  the UI just keeps listening. Set `stt_noise_filter: false` to disable.

## Turn detection on CPU (Smart Turn v3)

Since Pipecat 1.8 the agent closes a turn with **Smart Turn v3** (the
v3.2 weights bundled in the wheel) — a small semantic end-of-turn model
run on CPU with onnxruntime — instead of a silence timer. What it costs, measured on a
4-core ARM VM with no GPU (`tests/test_turn_hardware.py` covers the sizing;
the numbers come from `LocalSmartTurnAnalyzerV3._predict_endpoint` on 8 s
of audio):

| Stage | When it runs | Cost |
|---|---|---|
| Silero VAD | every 32 ms audio chunk | ~0.24 ms per chunk (≈0.7 % of one core) |
| Smart Turn v3 verdict | **once per pause** (VAD stop), not per frame | ~60–90 ms single-threaded |
| same, BLAS/OMP left uncapped | | 90–130 ms — *slower*, and it takes every core |
| model load | once per WebSocket conversation | ~50–90 ms |

Two things follow, and the agent does both by itself:

- **Thread caps.** The verdict's feature step is an 8 s Whisper-style log-mel
  computed in numpy; uncapped, OpenBLAS/OpenMP fans it across every core,
  which is slower for a 1×8 s input *and* steals cores from Whisper, Piper
  and Ollama next door. `camera_agent.py` sets
  `OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=1` before numpy
  loads (the Dockerfile sets the same); an explicit value in the environment
  wins. onnxruntime has its own pool — `turn_cpu_threads` — and **1 thread is
  the fastest setting up to 7 cores**; auto uses 2 from 8 cores.
- **Hardware profile.** `turn_detector: auto` (the default) looks at the cores
  *this process may use* — scheduler affinity **and** a cgroup CPU quota
  (`docker run --cpus`, compose `cpus:`), which `os.cpu_count()` ignores —
  and runs Smart Turn whenever there are ≥ 2. On a single core it falls back
  to a plain silence timer (`turn_timer_secs`, default 0.8 s; no model at
  all). The startup log prints the resolved choice
  (`turn-taking: Smart Turn v3 … 1 onnxruntime thread(s) … 4 core(s)`), and
  `GET /hardware` returns it under `turn`.

Knobs (`config.yml`):

```yaml
turn_detector: auto      # auto | smart | timer
# turn_cpu_threads: 1    # onnxruntime threads per verdict (auto: 1, 2 from 8 cores)
# turn_timer_secs: 0.8   # timer mode: silence that ends a turn
turn_max_secs: 8.0       # the model only ever sees the last 8 s; more is pure cost
turn_stop_secs: 2.0      # after this much silence the turn ends regardless
```

Memory is negligible (the model is ~8 MB; a 16 kHz int16 buffer for
`turn_max_secs` is 256 KB). The one thing worth knowing on a busy box: the
verdict runs on the pipeline's event loop thread pool, so a 60–90 ms pause
verdict is the floor of the "you stop → agent starts" latency; the Whisper
transcript for the turn is usually still in flight at that point, so it
rarely adds to the wall clock.

## Thinking aloud (`fillers.py`)

**Off by default since 2026-11-09** (`thinking_aloud: false`). It was built
for the CPU-only box, where a tool turn took 5–10 s. On the Ventuno Q's NPU
the answer lands in one or two seconds, and "Let me check the office
camera" right before it read as an unexpected extra reply in the morning
test of 2026-11-09. Everything below still applies when it is turned on.

A person who has to look something up says what they are about to do,
then goes quiet and does it. The agent knows *what it is about to do* the
moment the first LLM pass returns a tool call — the tool and its
arguments — and that is the only moment worth filling: the tool, the
second LLM pass and TTS are the slow part. So when a voice question needs
a slow tool it says, from the call's own arguments, "Let me check the
gate camera for vehicles between 2 and 3" / "Let me take a look at the
front door camera — is the door open" / "Let me check the plate reads for
6 6 H H 0 7 in the last hour", and the line plays while the tool runs.

What it costs: no LLM (one sentence shape per tool, slots filled from the
arguments the model already extracted), one short Piper synthesis run in
parallel with the tool and cached by text, one message on the page's
`/updates` socket. Nothing is added to the answer's path; the answer cuts
the line if it lands first. On a CPU-only box Piper is the CPU hog, so
the parallel synthesis is the one real cost — cached lines are free, and
typed questions never synthesise (status text only).

When it speaks: at most once per turn, never for instant lookups or
control verbs (`recent_events`, `create_alarm`…), and only when the
expected wait — the agent's own recent medians for that tool + the second
LLM pass + TTS, seeded from defaults — is at least `filler_min_ms`
(1500). On a GPU box where a history search plus reply takes a second,
it stays quiet. `GET /thinking-aloud` lists the recent decisions with
their reasons. `filler_source: model` asks the LLM to write the line in
the *same* first pass (about ten extra output tokens) and falls back to
the template when the model's line is missing, long, or is the answer
itself — worth trying with your model; the template is the safe default.

## Recommended model choices

| Role | Default (snappy) | Upgrade (quality, slower) | Why |
|------|------------------|---------------------------|-----|
| LLM | sized by the installer: `granite4:micro` on a CPU with 4+ cores or a GPU, `qwen2.5:0.5b` where RAM or cores are short | `qwen3:1.7b` when seconds matter more than the Investigate step (about half the time a turn) | Must support tool-calling — see "Measured: which small LLM picks the right tool" below. The installer sizes this to the machine. `qwen2.5:0.5b` is the low-RAM floor. |
| STT | Parakeet TDT 0.6B v3 (`STT_ADAPTER=parakeet`) | whisper `small.en` for translation or Hindi (`STT_ADAPTER=whisper`) | Measured on the agent's phrasings: WER 1.4 % at 0.31 s an utterance on four cores against whisper base.en's 3.3 % at 1.42 s. |
| TTS | Piper `en_US-lessac-medium` (`PIPER_VOICE`) | `en_US-libritts-high` — 2-3x the compute | Piper is the CPU hog of the stack; the tier is the cost (rule 3). Pick the voice to match the persona gender. |
| Detect | YOLO26n (`yolo26n.onnx`; yolov8n and yolo11n are drop-ins) | YOLO26s/m | n is the fastest (62 ms a frame on a 4-thread CPU); larger nets cost latency per frame and per poll. |
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


## Running the LLM on an NPU (the qualcomm flavour)

On a Qualcomm box the litertlm caption adapter answers `/v1/chat/completions`
on the same Gemma 4 E2B engine it captions with (ai-adapter #83), and
`start.sh` points the agent at it: `LLM_PROVIDER=openai`,
`LLM_BASE_URL=http://caption-adapter:9006`, `LLM_WINDOW_TOKENS=4096`, the
bundled Ollama skipped. Measured on a Ventuno Q with the agent's real system
prompt and seven of its tool schemas
(`bench/ventuno-q-npu-agent-llm-2026-11-07.md`): 10 of 10 right tool
choices on the evaluation questions, twice over, at 0.8–2.0 s a turn, where
granite4:micro on the same box's CPU takes 20–25 s and 3 m 50 s to warm up.

The catch is the window. The bundle is compiled with 4,096 tokens and the
runtime refuses a longer prompt instead of truncating it; the agent's full
prompt (system text plus fourteen schemas plus eight messages of history,
about 5,000 tokens) does not fit. `llm_window_tokens` makes each turn send
what fits (`prompt_budget.py`): the tool schemas the question is likely to
need — the router's hint first, then the tools the words point at (an
alarm word, a past tense, "apps"), then a fixed priority order whose first
seven are the set the spike ran — and the newest history in whole pairs,
two turns at least. A tool left out cannot be called that turn, the same
as the cloud config's `enabled_tools`, only decided per question. The
prefix cache that rules 1–4 above protect does not exist on this path (a
conversation is created per call and prefills in well under a second), so
a changing tool list costs nothing here; leave `llm_window_tokens` at 0
on an Ollama host, where it does.

### The live look after the LLM moved (8 November 2026)

With the model on the NPU the slow part of a live question is no longer
the model. The 32-case eval on the Ventuno Q: `detect_objects` 5.6–5.8 s,
`describe_camera` 8.0 s, of which the detector was 0.1 s, the caption
1.7 s and the compose answer 0.5–0.9 s. The rest was the agent opening its
own RTSP session for a fresh frame (connect, wait for a keyframe, decode:
"live frame from cam23 … 6229 ms" in the system check), which it does
whenever no Tier-0 track is active — a quiet room, every time. The
pipeline decodes that stream all day, so it now serves its newest decoded
frame at `/latest_frame` and the agent's frame cache asks for it first;
the RTSP grab stays as the fallback for a camera the pipeline does not
decode or a stream that went stale.

## What the installer suggests (hardware-aware defaults)

The installer detects the machine that will RUN the LLM (host vs the
Docker VM) and sizes the defaults to it. Suggestions only — type any
Ollama model at the prompt. Keep this table in sync with
`suggest_models` / `suggest_whisper_model` in `scripts/install.sh` and
the equivalent logic in `scripts/install.ps1`.

Both Ollama models are **resident at once** — the shipped compose sets
`OLLAMA_KEEP_ALIVE=-1` so answers stay snappy — and they share the box
with the OpenNVR stack. So the installer budgets them **together**:

```
model budget = RAM − 3 GB (the stack: 13 containers hold ~2 GB RSS,
                           plus Docker Desktop's VM overhead)
                   − 2 GB (the operator's own machine)
```

The LLM is chosen first, from the largest *tested* catalog entry that
fits both the budget and a speed ceiling; the vision model gets what is
left — **and only on a CUDA GPU.** Everywhere else scene description
goes to the `moondream` **adapter** — a ~0.5b-int8 build on onnxruntime
in its own container, far smaller than anything in the Ollama catalog
and still a real VQA answer.

Vision through Ollama is a GPU path, full stop. Ollama's `moondream` is
the 1.8B checkpoint: on an 8-core CPU it took 30 s+ per image, core's
background enrichers gave up at 15–20 s, and the model finished every
abandoned answer anyway — two cores burnt continuously for zero captions
saved (open-nvr #583). The 0.5B adapter answers the same questions
roughly ten times faster on the same cores. Apple Silicon is on the CPU
side of this line on purpose: Metal makes the host LLM fast, but the
caption adapter runs inside the Docker VM with no GPU either way.

Sizing them independently is what put a 4 GB vision model on 8 GB
machines: `gemma3:4b` was gated on `RAM ≥ 8` alone, so an 8 GB box got
5 GB of models on top of a 3 GB stack and thrashed.

| Machine | LLM | Vision | Adapter | Whisper |
|---|---|---|---|---|
| 4 GB / 2 cores, CPU | `qwen2.5:0.5b` | — | `moondream` | `tiny.en` |
| 8 GB / 4 cores, CPU | `granite4:micro` | — | `moondream` | `base.en` |
| 8 GB / 8 threads, CPU | `granite4:micro` | — | `moondream` | `base.en` |
| 16 GB / 8 threads, CPU | `granite4:micro` | — | `moondream` | `small.en` |
| 16 GB / 4 cores, CPU | `granite4:micro` | — | `moondream` | `base.en` |
| 32 GB / 16 cores, CPU | `granite4:micro` | — | `moondream` | `small.en` |
| 8 GB, GPU | `granite4:micro` | — | `moondream` | `base.en` |
| 16 GB, GPU | `granite4:micro` | `qwen3-vl:2b-instruct` | `ollamavlm` | `small.en` |
| 32 GB, Apple Silicon | `granite4:micro` | — | `moondream` | `small.en` |
| detection failed | `qwen2.5:0.5b` | — | `moondream` | `tiny.en` |

The resolved captioner decides whether core describes every recorded
visit in the background (`EVENTS_CAPTION_ENRICHMENT`,
`EVENTS_DESCRIPTOR_ENRICHMENT`): on without asking when it is
`ollamavlm` on a CUDA Ollama, asked and defaulting off for every CPU
captioner — including the in-container `moondream` a CUDA box falls
back to when RAM is short. Visits recorded while it is off can be
described later by setting both flags back to true together with
`EVENTS_ENRICHMENT_BACKFILL=true` (the backfill honours the same flags).

Detection failing sizes DOWN, never up: an unknown machine is treated as
a small one, because the cost of guessing high is an install that does
not run.

CPU-only machines are tiered by cores as well as RAM, because "it fits"
and "it answers before the operator gives up" are different questions —
and a machine with fewer than 4 cores skips the Ollama vision path
entirely, whatever the RAM arithmetic says.

The vision model carries that same speed ceiling, not just a RAM one. A
4B vision model costs ~25 s per caption on CPU against ~1-2 s on a GPU,
so CPU-only machines are capped at the catalog's *fast* tier however
much RAM is spare. Without that cap every machine with room landed on
`gemma3:4b` — it fits, so it won — and the suggestion stopped varying
with the hardware at all.

Two constraints shape the tiers. The **floor** is tool-calling quality:
below ~1.5b the agent misroutes tools noticeably, so the tiny tier is
only suggested where latency would otherwise make the agent unusable.
The **ceiling** is the tested envelope: defaults never exceed the
largest model this agent has actually been exercised with — "this
hardware can run a bigger model" is detectable, "bigger will be better
for this agent's tool-calling and voice latency" is not. The
`qwen3:1.7b` + `gemma3:4b` pair entered that envelope by exactly this
route: field-tested against the `qwen2.5` + `moondream` defaults with
this agent, found clearly better at similar cost, then promoted.
Machines with headroom (≥ 32 GB GPU-backed) are told at the prompt that
`qwen2.5:7b` and `qwen2.5vl:3b` are worth trying; promoting them to
defaults should follow the same testing route, not RAM arithmetic.


## Measured: which small LLM picks the right tool

`tools/eval_harness.py` run over its nine cases, five repeats each, once with
the router on (as shipped) and once with both router settings off (the model
on its own). One 4-vCPU cloud VM (Intel Xeon 2.1 GHz, 17 GB, **no GPU**),
Ollama 0.40, text mode, the `config.docker.chat.yml` prompt and tools, one
still-image camera and **no vision adapters** — so this scores tool choice
and arguments, not the quality of a description. Raw numbers per case:
[`benchmarks/llm-tool-choice-cpu-4vcpu-2026-10.json`](benchmarks/llm-tool-choice-cpu-4vcpu-2026-10.json).

| Model | Resident | Pass, router on | Pass, router off | **Model chose it** (off) | Alarms armed | Quiet on "thanks" / "hi" | p50 per turn (on / off) |
|---|---|---|---|---|---|---|---|
| `llama3.2:3b` | 3.5 GB | 29/45 | 30/45 | **30/45** | **10/10** | 4/10 | 36 s / 40 s |
| `qwen3:1.7b` | 2.4 GB | **30/45** | **30/45** | 10/45 | **10/10** | 0/10 | 8 s / 9 s |
| `qwen2.5:3b` | 2.7 GB | 25/45 | 25/45 | 25/45 | 5/10 | 0/10 | 9 s / 13 s |
| `qwen2.5:1.5b` | 1.6 GB | 21/45 | 23/45 | 8/45 | 0/10 | 2/10 | 5 s / 6 s |
| `qwen2.5:0.5b` | 0.7 GB | 23/45 | 13/45 | 12/45 | 0/10 | 2/10 | 2.5 s / 8 s |
| `phi4-mini` | 4.3 GB | 23/45 | 22/45 | 2/45 | 0/10 | 2/10 | 17 s / 18 s |
| `granite3.3:2b` | 2.4 GB | 20/45 | 21/45 | 1/45 | 0/10 | 0/10 | 13 s / 15 s |
| `qwen3:0.6b` | 1.5 GB | 20/45 | 20/45 | 0/45 | 0/10 | 0/10 | 7 s / 8 s |
| `llama3.2:1b` | 2.2 GB | 16/45 | 5/45 | 5/45 | 0/10 | 0/10 | 16 s / 19 s |
| `qwen3:4b` | 4.3 GB | — | 20/45 | 0/45 | 0/10 | 0/10 | — / 103 s |

*Resident* is what Ollama reports loaded, KV cache for `llm_num_ctx: 8192`
included. *Model chose it* counts the turns where the model itself called
the right tool with the right arguments — not the router (tier 0) and not the
anti-fabrication guard's forced call. *Alarms armed*: the two alarm cases,
where the alarm must exist afterwards with the right target and window.

What it says:

- **The model rarely does the camera work itself.** With the router off,
  most passes on camera questions are the forced grounding's, not the
  model's: the guard, not the model, is what keeps small models honest.
- **`llama3.2:3b` is the only model that does most of it itself** (30/45,
  every alarm armed) — the pick where an accelerator pays for its size; on
  this CPU it is ~4× slower per turn than `qwen3:1.7b`.
- **`qwen3:1.7b` is the CPU pick**: the best end result at ~9 s a turn, and
  it arms alarms. `qwen2.5:1.5b` and `0.5b` never armed one — they leave out
  `target`, so the agent asks back and nothing is created.
- **Size alone does not buy tool use:** `phi4-mini`, `granite3.3:2b`,
  `qwen3:0.6b` and `llama3.2:1b` almost never chose a tool themselves.
- **`qwen3:4b` as Ollama ships it reasons in its answer even with
  `think: false`** (~100 s a turn, no tool calls). Leave it out until that
  changes.
- **Two failures do not depend on the model**, so they are agent fixes, not
  model picks: the forced grounding fires on "thanks" / "hi" (it also reads
  the model's reply for camera words), and "notify me when more than 3 people
  gather" puts the camera in `target` for nearly every model.

One CPU, five repeats: read differences of a case or two as noise (the JSON
carries a 95% interval per case). To add your hardware, run the same
harness against your agent (see `AGENT_DESIGN.md`, "Measuring better") with
the router on and off.

### Measured again, 16 October 2026: the catalog's top five

The table above is the run that chose `qwen3:1.7b`. Two things changed
since: the agent (the router now handles small talk, the forced-grounding
reads the reply) and the question — the Investigate step of Agentic
Workflows, where the model has no router and no guard and must call the
tools itself. The candidates were run on both harnesses: Investigate
(`tools/investigate_eval.py`, 20 cases × 3 repeats, recorded tool
results, no NVR) and tool choice (`tools/eval_harness.py`, 9 cases × 5
repeats, a live agent on one still-image camera). Accuracy was measured
on a Mac and reproduced the 4-vCPU VM's number for `qwen3:1.7b` exactly
(21/60), so the pass columns compare; the seconds do not, and are
given as a ratio against `qwen3:1.7b` on a 4-thread CPU instead.

| Model | Resident | Investigate pass | Looked first | Tool choice, router on | Seconds a turn vs `qwen3:1.7b` |
|---|---|---|---|---|---|
| **`granite4:micro`** | 2.1 GB | **44/60 (73%)** | **56/60** | 35/45 → **45/45** with the small-talk tier | ~2× |
| `granite4:tiny-h` (7B MoE, 1B active) | 4.2 GB | 35/60 | 49/60 | — | — |
| `granite4:micro-h` | 2.1 GB | 34/60 | — | — | — |
| `qwen3:1.7b` | 1.4 GB | 21/60 (35%) | 36/60 | 45/45 | 1× |
| `LFM2-1.2B-Tool` | 1.3 GB | 19/60 | 37/60 | — | ~0.7× |
| `qwen3:4b` | 2.5 GB | 9/60 | 9/60 | — | ~5× |
| `xLAM-2-3b-fc` | 1.9 GB | 3/60 | — | — | its output format fights Ollama's schema mode (21 errors); not a fair run |

What it says:

- **`granite4:micro` is the default.** It more than doubles the
  Investigate pass rate for one reason that is visible in the traces: it
  calls the tools it is given (2.1 per step against 0.8) and then
  decides, where `qwen3:1.7b` guesses and the bigger `qwen3:4b` reasons
  without looking at all (zero tool calls in most runs). It still misses
  the Investigate gate (the lower bound must reach 0.80); four of its
  sixteen misses are one case where it wants a fifth tool call and the
  CPU limit is four.
- **Its two chat failures were the agent's, and are fixed.** On "thanks,
  that's all" and "hi, what can you do?" it obeyed "always call a tool
  before describing" to the letter and ran a three-tool sweep. Small talk
  is now a router tier with no tools (the table at the top), for every
  model.
- **The cost is time**: about twice `qwen3:1.7b`'s seconds a turn on CPU
  (prompt 104 against 214 tokens/s, generation 40 against 67, four
  threads). On the 4-vCPU VM that is ~20-25 s a turn; `qwen3:1.7b` stays
  in the catalog as the fast pick.
- **Bigger Qwen is worse here, not better**, and `gemma3n` could not be
  run (it needs a newer Ollama than 0.35). The remaining catalog entries
  are the measured top five; the rest were dropped.

## Measured: can a small model run an Investigate step?

RFC #620 offers a workflow's **Investigate** step on a site only when its
language model passes the Investigate cases. `tools/investigate_eval.py`
runs them: each case is one AI step (a goal, site facts, the event as
data, at most five read tools, then one decision call constrained to the
step's options), with **recorded tool results**, so no NVR is involved and
only the model varies. Each step runs through the SDK's
`run_investigate()`, the code a workflow runs, so the gate measures what
ships. There is no router and no forced grounding: in a workflow, every
tool call is the model's own.

```bash
python tools/investigate_eval.py --model qwen3:1.7b --hardware cpu --repeat 3
```

A run passes when the decision and its fields are right **and** the tools
a good answer needs were called; a right answer without looking is a
guess. The gate is the Wilson lower bound of the pass rate (>= 0.80), with
p95 inside the hardware class's seconds (CPU 4 tool calls / 120 s, GPU
6 / 45 s). Same VM as above. Raw runs:
[`benchmarks/investigate-cpu-4vcpu-2026-10.json`](benchmarks/investigate-cpu-4vcpu-2026-10.json).

| Model, mode | Runs | Pass | 95% interval | Looked first | p50 / p95 per step | Qualifies |
|---|---|---|---|---|---|---|
| `granite4:micro`, CPU limits (Mac, accuracy only) | 60 | **44 (73%)** | 0.61-0.83 | 56/60 | — | **no**, the closest yet |
| `granite4:micro`, CPU limits (4 vCPU, through `run_investigate`, corrected cases) | 60 | **45 (75%)** | 0.63-0.84 | 59/60 | 30 s / 44 s | **no**, fits the time with room |
| `qwen3:1.7b`, CPU limits (corrected cases) | 60 | 16 (27%) | 0.17-0.39 | 36/60 | 12 s / 22 s | **no** (21/60 before the cases were corrected) |
| `llama3.2:3b`, no time limit | 20 | 10 (50%) | 0.30-0.70 | 12/20 | 24 s / 39 s | **no** |
| `qwen3:1.7b`, decision in the tool call (#649's harness, `--combined`) | 60 | 0 (0%) | 0.00-0.06 | 6/60 | 5 s / 8 s | **no** |

What it says:

- **No CPU model qualifies for Investigate yet**; `granite4:micro` is the
  closest (see "Measured again"). Not for lack of time:
  both Qwen runs fit the 120 s limit with room to spare. They decide without
  looking: about four runs in ten end without the tools the answer needs,
  and the reason then cites evidence that was never gathered ("the EVENT
  data shows a guard in a vest"). This is the camera agent's finding again
  (the model rarely does the camera work itself), without the guard that
  covers for it there. **Look on CPU, Investigate on an accelerator** is
  the launch shape the data supports.
- **The decision needs its own call.** With the schema passed alongside
  the tools, Ollama's constrained decoding produces JSON from the first
  token, so the model can never call a tool: zero tool calls in 60 runs,
  the first option nearly every time. The SDK loop keeps the tool phase
  and the decision call separate (design note on #620, open question 2).
- **The model's own confidence does not separate right from wrong.**
  AUROC is 0.59 for qwen3:1.7b's self-reported number and 0.53 for
  agreement across three decision draws (0.5 is chance); its right and
  wrong answers both have a median self-reported 0.9. Too few runs to pick the default
  confidence source yet, but enough to say a floor on the self-report
  would not gate anything.
- The camera-agent pick for tool choice, `llama3.2:3b`, does better
  (half the cases) but not near the gate, on one repeat.

- **Why granite4:micro still fails: it looks, then decides against what it
  saw.** Of its 15 failing runs, all but one called the tools it needed
  (59/60). The decision contradicts the run's own reason: "authorised,
  matches the bakery schedule" then *alert*; "tampering by a person with a
  spray can" then *notify_maintenance* where the instructions say tampering
  is an alert; "the gate was open" then *all clear*; an event read as "a
  person in a dark hoodie carrying a box away" then "all patrols
  recognised". Restating the step's instructions in the decision message,
  right before the answer, did not help: granite4:micro 45/60 both ways,
  qwen3:1.7b 16 to 19 (within noise). Not shipped.
- **The cases were corrected after a failure analysis** (October 21). A
  `describe_event` fixture answered any event id, so an id the model made
  up came back as "a guard on patrol"; it now answers only ids the case
  printed, as the real tool does, and a test keeps it so. A search with no
  label now returns what is there. Two expectations were too strict. The
  correction left granite4:micro at 45/60 and took qwen3:1.7b from 21 to
  16: it had been rewarded for inventing ids.

One CPU, few repeats: the intervals are wide on purpose, and two identical
runs differ by one or two passes on a single case, so read totals, not
cases. To add a GPU result, run the same command with `--hardware gpu`.

### Facts + rules, on cases the rules never saw (22 October 2026)

With a rule table (`--rules tools/investigate_rules.yml`) the model never
chooses the outcome: it reports the template's facts, and the first
matching rule decides (RFC #620). The table is each template's
instructions written as rules, frozen **before** the 20 held-out cases
(`tools/investigate_cases_heldout.yml`) were written: the rules' commit is
authored 10:23 UTC on 22 October, the cases' 10:25 (`git log --follow` on
both files). Raw runs:
[`benchmarks/investigate-rules-heldout-2026-10.json`](benchmarks/investigate-rules-heldout-2026-10.json).

| CPU limits, 20 cases × 3 | Seen, model decides | Seen, rules decide | **Held out, model decides** | **Held out, rules decide** |
|---|---|---|---|---|
| `granite4:micro` | 45 (0.63-0.84) | 51 (0.74-0.92) | **29 (0.36-0.61)** | **30 (0.38-0.62)** |
| `qwen3:1.7b` | 16 (0.17-0.39) | 24 (0.29-0.53) | **15 (0.16-0.37)** | **17 (0.19-0.41)** |

What it says:

- **The gain did not hold on held-out cases.** Paired run by run, the
  rules fixed 6 and broke 5 granite4:micro runs (sign test p = 1.0), and
  fixed 4, broke 2 for qwen3:1.7b. On the decision and fields alone,
  with the needed-tools check aside, they lean the right way (7 fixed /
  3 broken, 7 / 2) but not beyond noise. On the seen cases the rules
  fixed "right facts, wrong action"; on fresh ones most failures are
  **wrong facts** (condensation reported as a power fault, a half-open
  shutter reported as nothing left open, "yes" to a name the history
  does not contain, no history search at all), and a rule cannot fix a
  wrong fact.
- **The seen cases flatter the model.** granite4:micro passes 45 of 60
  on them and 29 on fresh ones, with nothing else changed. It was chosen
  as the default on those 20 cases, and the cases were corrected with it
  in view; the held-out number is the honest one. Read 45/60 as an
  upper bound.
- **Rules are still the safer shape**, for a different reason than
  accuracy: a step's outcome can no longer contradict its own facts, and
  the trace says which rule fired. That is a design choice, not a
  measured accuracy gain.
- One held-out case is too strict and is left as written (held-out cases
  are not edited after their results): `ho-offline-turned-to-the-sky`
  requires the live camera, but the stored event is equal evidence of
  tampering. It costs the rules run one pass.

## The Look eval (`tools/look_eval.py`)

RFC #620 offers Look everywhere; its gate comes from Look cases on a
site's own pictures. `tools/look_eval.py` runs each picture through the
workflow's Look step (`tools/look_templates.yml`, written as an `ai:`
step and run by the SDK's `run_ai_step`, so it measures what ships) and
gates on the Wilson lower bound of the pass rate (>= 0.80) with p95
inside the hardware class's seconds, per template.

The labels are a CSV a person can keep in a spreadsheet
(`tools/look_cases.example.csv`): the file, the template, and the right
answer, as the decision (`delivery`), facts (`who=guard`) or both
(`delivery; company=swiggy`). With a rule table, a fact label is enough:
`who=stranger` expects `alert`. A run passes when the decision and every
fact the label names are right.

    python tools/look_eval.py --cases site/cases.csv --check      # labels only
    python tools/look_eval.py --cases site/cases.csv --model qwen3-vl:2b-instruct \
        --hardware cpu --repeat 3 --out look.json
    python tools/look_eval.py --cases site/cases.csv --rescore look.json  # after fixing a label

A site's pictures stay out of the repository; the run file keeps names,
answers, timings and, when the model's output was no answer, what it
said.

**No accuracy yet**: that needs the site pictures. A plumbing check on 4
coco128 frames (2 dogs, 2 scenes with nobody; `yard_threat`, CPU, 4
threads), which says nothing about accuracy but two things about the
step:

- **Seconds**: `qwen2.5vl:3b` took 47-53 s a step; `qwen3-vl:2b-instruct`
  37-49 s on the first pass and 13-20 s on reruns of one frame. Inside
  CPU's 120 s, but a CPU Look step is tens of seconds, not the 9-12 s of
  a text step; the site run will give the real p95.
- **Two failures to watch on the real cases**: `qwen2.5vl:3b` wrote "a
  small dog" in its reason and `who: nobody` in the field (the
  Investigate finding again, right observation and wrong fact); and
  `qwen3-vl:2b-instruct` twice in 8 runs wrote a reason long enough to
  run out of tokens before the JSON closed (`invalid_output`). The second
  is a fix for the SDK (a tighter reason, or more tokens for Look),
  measured on the site cases before and after.

### The public night-yard cases (30 October 2026)

`tools/look_cases_public.csv`: 58 `yard_threat` cases from openly
licensed datasets, fetched by `tools/fetch_look_public.py` (no picture is
in the repo): 20 night infrared animals (ENA24 via LILA BC,
CDLA-Permissive), 18 people and 20 empty scenes from MEVA's CCTV
(Kitware / IARPA, CC BY 4.0). The people are daytime actors, the empty
scenes thermal or midnight frames; no public set has a guard on patrol.
A first number for the step, **not a site's gate**: the cameras are not
the site's, and public pictures may be in a model's training data.

`qwen3-vl:2b-instruct`, CPU limits (120 s), 4 threads, one repeat. Raw
runs: [`benchmarks/look-public-yard-2026-10.json`](benchmarks/look-public-yard-2026-10.json).

| Pictures as sent | Pass | 95% interval | p50 / p95 per step | Animals | Strangers | Nobody | Ran out of time | Output cut off |
|---|---|---|---|---|---|---|---|---|
| Full size (MEVA 1920×1072, ENA24 1920×1080) | 26/58 (45%) | 0.33-0.58 | 92 s / 120 s | 1/20 | 10/18 | 15/20 | 18 | 6 |
| `--max-side 768` | **35/58 (60%)** | 0.47-0.72 | **40 s / 52 s** | **17/20** | 6/18 | 12/20 | 0 | 8 |

What it says:

- **Picture size is the CPU cost.** A vision model encodes every pixel it
  is sent; a 1080p snapshot alone took most of a step's 120 s, and 18 of
  the 20 animals ran out of time. At 768 px on the long side, p95 fell to
  52 s and 17 of the 20 were right. **A Look step on CPU should shrink
  the picture before the model sees it**; the engine is the place (the
  SDK has no image library), `--max-side` measures it here.
- **Strangers become guards.** At 768 px, 6 of the 18 daytime people
  came back `who: guard`, so the rules said `ignore`: the failure that
  matters most on this step. Whether a site's night cameras do better
  needs the site's pictures and a real guard, which no public set has.
- **Empty thermal scenes become strangers** (5 of 20 at 768 px).
- **Reasons that run on**: 6 and 8 runs in 58 wrote a reason long enough
  to run out of tokens before the JSON closed (`invalid_output`), the
  failure the coco128 check showed. An SDK fix, measured on these cases
  before and after.
- Strangers and empty scenes did a little worse at 768 px than at full
  size; with one repeat that is within noise, and three repeats will say.

## Measured: the vision models on coco128 (16 October 2026)

`tools/vqa_eval.py` asks each model the agent's questions on coco128's
128 frames and scores them against the labels: *present* ("is there a
<class in the frame>?" → yes), *absent* ("is there a <class not in it>?"
→ no; a yes is a hallucination, the failure that matters on a camera),
*count* ("how many people?", exact and within one). Accuracy on a Mac
GPU; the seconds column is a 4-thread CPU run on 12 frames. The shipped
moondream is the adapter container (0.5b int8, CPU inside Docker),
measured over its own `/infer`; Ollama's moondream answered nothing on
Ollama 0.35.1 here and is not in the table.

| Model | Present | Absent (no false yes) | Count exact / within one | Unanswered | CPU, a new frame | CPU, a follow-up |
|---|---|---|---|---|---|---|
| `moondream-adapter` (shipped, CPU default) | 117/126 | 104/126 | 27 / 42 of 61 | 0 | 7.6 s | 7.6 s |
| **`qwen3-vl:2b-instruct`** (GPU default) | 115/126 | **126/126** | **38 / 50** of 61 | 0 | 18.2 s | **0.16 s** |
| `gemma3:4b` (the previous GPU default) | 113/126 | 118/126 | 33 / 48 of 61 | 0 | 16.8 s | 16.8 s |
| `qwen3-vl:2b` (the thinking tag) | 116/126 | 126/126 | 34 / 39 of 61 | 27 | 19.4 s | 1.2 s |
| `qwen3-vl:4b` (thinking) | 111/126 | 123/126 | 35 / 39 of 61 | 27 | — | — |

What it says:

- **Moondream keeps CPU.** It is the only one that answers in
  single-digit seconds on four cores, and it answers; its weakness is
  false yeses (22 of 126), which matters less than it looks because the
  agent never asks a VLM "is there a person" or "how many" — those go to
  the detector. A better answer after the moment is a worse answer.
- **`qwen3-vl:2b-instruct` takes the GPU slot** (17 October): no false
  yes in 126, the best counter, nothing unanswered, 3 s a frame on a
  GPU and every further question on that frame free — better than
  `gemma3:4b` on every axis and a gigabyte smaller. Its one cost is
  encoding the frame, ~18 s on four CPU cores, which is why it is not
  the CPU pick: Moondream is 2.4× faster for a single question. A step
  that asks several questions of one frame (Investigate) crosses over
  after the third question even on CPU — a routing option for later.
- **The plain `qwen3-vl:2b` tag is the thinking build** and cannot be
  told not to think (27 of 313 answers ran out of budget with nothing
  said); the `-instruct` tag is the same model without it. The
  earlier run was that tag, not Ollama ignoring `think: false`.
- Not measurable through Ollama here: LFM2-VL (no library entry, the
  GGUF would not import) and Florence-2 (a caption/grounding model, not
  a VQA one; it would be its own adapter).

## Measured: speech (16 October 2026)

`tools/stt_eval.py` on 29 of the agent's own phrasings (the eval cases
plus twenty of the same kind) spoken by four macOS voices
(`tools/stt_synthesise.sh`), scored with Whisper's English normaliser so
"10 pm" and "10pm" are not errors. Every number from one 4-CPU container.

| Model | WER | Per utterance p50 / p95 | Licence |
|---|---|---|---|
| **Parakeet TDT 0.6B v3** (onnx-asr, int8) — `STT_ADAPTER=parakeet` | **1.4 %** | **0.31 s / 0.50 s** | CC-BY-4.0 weights |
| faster-whisper `base.en` int8 — the whisper adapter's default | 3.3 % | 1.42 s / 1.68 s | MIT |
| faster-whisper `small.en` | 3.7 % | 2.67 s / 2.84 s | MIT |

Half the errors at a fifth of the time, on the phrasings that matter.
Parakeet's misses were "load and bay" and "camera too"; whisper's
"neither gate", "off to 10pm", "said an alarm". A second of STT a turn
was the largest single cost in the latency budget above on CPU, which
is why Parakeet is the default. It does not translate and has no Hindi;
`STT_ADAPTER=whisper` keeps those.

## Measured: voice (16 October 2026)

`tools/tts_eval.py`: twenty replies the agent actually gives, through
each engine on four CPU cores. Real-time factor is generation seconds
over audio seconds; under 1 the reply can play while it is made.
Intelligibility is the audio back through whisper `small.en`.

| Engine | Real-time factor p50 / p95 | A 2.6 s reply takes | WER back through whisper |
|---|---|---|---|
| **Piper** `en_US-lessac-medium` (shipped) | **0.23 / 0.29** | **0.64 s** | 4.4 % |
| Kokoro-82M (`af_heart`) | 1.87 / 2.57 | 4.8 s | 3.9 % |

Kokoro sounds better and is as intelligible, at eight times the compute:
slower than real time on four cores, so it cannot stream ahead of
playback. Piper stays; Kokoro is a GPU-tier option if one is ever
wanted.

## Measured: embeddings (16 October 2026)

`tools/retrieval_eval.py`: text-to-image retrieval on coco128, "a photo
of a <class>" for the 49 classes in at least three frames, relevance
from the labels. 4-thread CPU.

| Checkpoint (OpenCLIP) | mAP | recall@5 | Image embed | Dim | Params | Weights licence |
|---|---|---|---|---|---|---|
| ViT-B-32 / laion2b_s34b_b79k (CLIP, until October 2026) | 0.571 | 0.94 | 26 ms | 512 | 151 M | MIT |
| **ViT-B-16-SigLIP2-256 / webli** (the `clip` adapter now) | **0.672** | **1.00** | 57 ms | 768 | 375 M | Apache-2.0 |
| ViT-B-32-SigLIP2-256 / webli | 0.630 | 0.96 | 27 ms | 768 | 377 M | Apache-2.0 |
| MobileCLIP2-B / dfndr2b | 0.647 | 0.96 | 47 ms | 512 | 150 M | not commercial-clean |
| MobileCLIP2-S0 / dfndr2b | 0.631 | 0.92 | 204 ms (unreparameterised) | 512 | 75 M | not commercial-clean |

SigLIP 2 B/16 is the smallest checkpoint that beats CLIP by more than
noise; it costs twice the milliseconds an image, once per visit, and its
vectors are 768 wide. A store of 512-wide CLIP vectors is not comparable
with them, which is why the platform re-embeds history when the model
behind the adapter changes (`docs/ENRICHMENT.md`, "Changing the
embedding model").

## Model catalog (what each option is actually good at)

The installer renders this catalog as the model-selection menu, annotated
for your detected hardware. **Canonical source:**
[`model_catalog.txt`](model_catalog.txt) — edit there; this table is kept
in sync by hand. "Tested" means exercised with THIS agent's tool prompts
and voice loop; "untested" entries are known-good models that nobody has
validated with this agent yet — trying one and reporting back is a great
first contribution. The LLM notes quote the measurements above.

### LLMs (the agent's brain — tool calling is what matters)

| Model | ~RAM | Speed | Status | Good at |
|---|---|---|---|---|
| `qwen2.5:0.5b` | 1 GB | fastest | tested | The floor for a 2-core or 4 GB box: fastest answers, leans on the router; alone it chose the right tool 12/45 and armed no alarm |
| `granite4:micro` | 3 GB | medium | tested | **The default.** Best measured by a wide margin: Investigate 44/60 (`qwen3:1.7b` 21/60) because it looks before it decides; 45/45 on the chat cases with the router; about 2× `qwen3:1.7b`'s seconds a turn on CPU |
| `qwen3:1.7b` | 3 GB | fast | tested | The fast pick where seconds matter more than the Investigate step: 45/45 on the chat cases, 21/60 on Investigate; the agent disables its thinking mode |
| `granite4:micro-h` | 3 GB | medium | untested | The hybrid (Mamba) build of the default: 34/60 on Investigate, same RAM; not yet run on the chat cases |
| `granite4:tiny-h` | 5 GB | medium | untested | 7B mixture of experts, 1B active: 35/60 on Investigate at 4.2 GB on disk; for a box with RAM to spare; not yet run on the chat cases |

### VLMs (the agent's eyes — used via `CAPTION_ADAPTER=ollamavlm`)

These are Ollama-servable vision models; the installer annotates each
with whether it fits your detected hardware. (`moondream` here is
Ollama's moondream2 ~1.8b; the standalone moondream-adapter container
ships the smaller 0.5b-int8 build — both are the same family, tested,
and the low-RAM pick either way. `blip` remains available as a plain
captioner via its own adapter, outside this menu.)

| Model | ~RAM | Speed | Status | Good at |
|---|---|---|---|---|
| `moondream` | 3 GB | fast | tested | The CPU default. The shipped adapter (0.5b int8) measured: 117/126 present, 104/126 absent (22 false yeses), people within one 42/61, 7.6 s an answer on CPU |
| `qwen3-vl:2b-instruct` | 4 GB | medium | tested | The GPU default. Measured: 115/126 present, 126/126 absent (no false yes), the best counter (within one 50/61); 3 s a frame on a GPU then follow-ups free, ~18 s to encode a frame on four CPU cores. The plain `qwen3-vl:2b` tag thinks and cannot be told not to |
| `gemma3:4b` | 4 GB | medium | tested | The previous GPU default. Measured: 113/126, 118/126 (8 false yeses), within one 48/61; 17 s an answer on a 4-thread CPU, 5 s on a GPU |
| `qwen3-vl:4b` | 5 GB | medium | measured, not through the agent | The thinking build at 4b: 111/126, 123/126, within one 39/61, with the thinking cost |
| `qwen2.5vl:3b` | 5 GB | medium | untested | Best text READER of its size (signs, labels, plates) |

Picking for a security camera: **counting and "is something there" never
needs a VLM** (YOLOv8 does that); the VLM answers "what/who/how does it
look". `qwen3-vl:2b-instruct` is the measured all-rounder on an
accelerator; `moondream` covers the same cheaply on small boxes. Reach for `qwen2.5vl:3b` or
`minicpm-v` when your questions involve **reading** (plates, parcel
labels, uniforms with text); reach for the big ones only when answer
richness matters more than latency.
