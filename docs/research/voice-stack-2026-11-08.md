# The camera agent's voice loop on the Ventuno Q: why it feels slow, and what to change (2026-11-08)

A nine-minute office session on 2026-11-08 (one camera, the demo page, voice) felt sluggish and
unnatural. The agent's own turn log explains most of it, and a research pass over current on-device
speech components gives the options. Facts first, then the plan.

## What the box actually runs today

| stage | what | measured in the session |
|---|---|---|
| end of turn | the **demo page's own volume detector**: 0.9 s of silence ends the utterance (`SILENCE_MS=900`), then the clip is uploaded. Smart Turn v3 (the semantic end-of-turn model) is loaded and running on the box, but it serves only the streaming WebSocket pipeline, which the demo page does not use. | 0.9 s wait, every turn |
| speech to text | the **Parakeet** adapter (`parakeet-adapter:0.1.10`, NVIDIA Parakeet TDT 0.6B v3, multilingual), on the CPU, language set to English | 0.7–2.3 s; hallucinated Portuguese and nonsense English from ambient speech |
| the model | Gemma 4 E2B on the NPU (since #817/#824) | 1–5 s (two calls when a forced look follows) |
| text to speech | Piper, `en_US-lessac-medium`, on the CPU | 0.3–0.9 s |
| playback | the whole reply, only after every stage above has finished; the page ignores the microphone while the reply plays (no barge-in) | nothing heard for 3–7 s after you stop talking |

So two of the three "slow" seconds are not the model: the silence wait and the fact that nothing
streams. The behavioural failures (agreeing with the user against the detector, invented actions,
answering hallucinated transcripts, no closing) are fixed deterministically in #829.

## The plan, ranked by what it buys

1. **Move the demo to the streaming pipeline and speak early** (no new models). The streaming
   pipeline already exists (Pipecat, `/ws`). Sentence-by-sentence TTS starts speaking ~0.2–0.3 s
   after the first sentence is complete instead of after the whole reply; a short acknowledgement
   ("Looking at the front door…") is spoken from the tool handler while the tool runs; barge-in after
   2–3 words so a cough does not cut the agent off; Smart Turn v3 decides the end of turn with its
   fallback silence lowered from 2.0 s to ~1.0 s and 2–4 threads. Expected: first audio 1.5–2.5 s
   after you stop talking (3–7 s today), and you can interrupt.
2. **Speech to text off the CPU, onto the Hexagon.** Qualcomm publishes Whisper-Small (w8a16) numbers
   for this very chip (Dragonwing IQ-8275 = QCS8275): encoder 56.5 ms, 9.1 ms a token, ~65 MB — about
   0.2 s an utterance against 0.7–2.3 s now, and the CPU freed. It is a QNN context binary for ONNX
   Runtime's QNN execution provider, the same provider the YOLO detector uses; it needs an adapter
   image (ai-adapter) and the on-box check that the binary matches this Hexagon generation (v75).
   Gate it: decode only VAD-confirmed speech of 300 ms or more; drop segments by Whisper's
   no-speech probability, log-probability and English language probability; keep the phrase
   blocklist. Whisper-Large-V3-Turbo on the NPU is the accuracy step-up (0.7 s encoder; the best
   published numbers on Indian-accented English are large-v3's). CPU fallback if the NPU route
   stalls: Moonshine v2 Small (MIT, streaming, 123M).
3. **Text to speech: keep Piper, stream it.** Piper is the only small model well under real time on
   an ARM CPU; Kokoro-82M sounds better but runs 3–8× slower than real time on four Pi-class cores
   and would delay the first sentence. No open Indian-English voice exists in any of these; try a
   Piper `en_GB` voice beside `lessac` and pick by ear.
4. **End-of-turn model: Smart Turn v3 stays.** BSD-2, 8 MB, ~60 ms on one small ARM core, trained on
   23 languages including Hindi. LiveKit's turn detector is licensed for LiveKit Agents only.
5. **Later experiments:** Gemma 4 E2B's own audio encoder is resident on the NPU and does
   speech-in → text-out in one call; accuracy on Indian English and contention with captioning are
   unknown. Whisper-Large-V3-Turbo on the NPU for accuracy.

What each step needs: 1 is agent and demo-page code; 2 is a new adapter image and a measurement on
the box; 3 is a `.env` voice change and the streaming from step 1; 4 is a config change (two
numbers and a thread count).

## Research notes

The brief below was compiled on 2026-11-08 from vendor and author publications; nothing in it was run
on the box unless marked. Sources inline.

### Speech to text

| Candidate | Size | Latency / RTF on ARM | Quality | Licence | Runtime |
|---|---|---|---|---|---|
| Parakeet TDT 0.6B v3 (current) | 600M | 0.7–2.3 s an utterance on the QCS8275 CPU (measured) | 6.34 % avg Open ASR; 11.7 % WER at 0 dB SNR ([card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)) | CC-BY-4.0 | NeMo; sherpa-onnx offline only ([issue](https://github.com/k2-fsa/sherpa-onnx/issues/2918)) |
| **Whisper-Small w8a16 on the NPU** | 244M | IQ-8275: encoder 56.5 ms + 9.1 ms/token ([card](https://huggingface.co/qualcomm/Whisper-Small-Quantized)) | small-class WER; multilingual decoder, needs gating | Apache-2.0 | QNN context binary / ORT QNN EP |
| Whisper-Large-V3-Turbo on the NPU | 809M | IQ-8275: encoder 708 ms + 12 ms/token ([card](https://huggingface.co/qualcomm/Whisper-Large-V3-Turbo)) | best on Indian English (large-v3 ~7.1 % on Svarah: [Svarah](https://arxiv.org/pdf/2305.15760)) | Apache-2.0 | QNN / ORT QNN EP |
| Moonshine v2 Small / Tiny | 123M / 34M | 148 / 50 ms on Apple M3; no ARM figure ([paper](https://arxiv.org/html/2602.12241v1)) | 7.84 % / 12.01 % avg Open ASR | MIT | own ORT library, aarch64 wheels ([pypi](https://pypi.org/project/moonshine-voice/)) |
| distil-small.en / faster-whisper small.en | 166M / 244M | slower than real time on a Pi 5 (blog-grade) | 12.1 % short-form ([card](https://huggingface.co/distil-whisper/distil-small.en)) | MIT | CTranslate2 |
| sherpa-onnx streaming Zipformer | 20–80M | RTF 0.062 int8 on desktop ([docs](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html)) | noticeably below Parakeet ([issue](https://github.com/k2-fsa/sherpa-onnx/issues/3573)) | Apache-2.0 | sherpa-onnx, true streaming |

Rejecting non-speech and hallucinations: decode only VAD-confirmed speech of ≥300 ms; for Whisper
use `no_speech_threshold` 0.6, `logprob_threshold` −1.0, `compression_ratio_threshold` 2.4,
`condition_on_previous_text=False`, and drop low English-language probability
([faster-whisper](https://github.com/SYSTRAN/faster-whisper)); confident hallucinations ("Thanks for
watching") still pass, so keep the phrase blocklist. For Parakeet, sherpa-onnx exposes no confidence
for TDT and beam search hallucinates about a fifth of the time, so use greedy
([issue](https://github.com/k2-fsa/sherpa-onnx/issues/3267)).

### End of turn and barge-in

| Candidate | Size | CPU latency | Quality | Licence |
|---|---|---|---|---|
| Pipecat Smart Turn v3 (current) | 8M, 8 MB int8 | 59.8 ms on a 1-vCPU Graviton; 12.6 ms x86 ([Daily](https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/)) | 94.3 % English; 23 languages incl. Hindi | BSD-2 |
| LiveKit turn detector v1-mini | 140–250 MB RAM | committed after ~1 s | 27.8 % false cut-offs at a 300 ms budget ([eot-bench](https://github.com/livekit/eot-bench)) | LiveKit Agents only ([licence](https://scancode-licensedb.aboutcode.org/livekit-model-2024.html)) |
| adaptive silence only | 0 | 0 | early cut-offs on hesitations | — |

Products set it so: Pipecat VAD `stop_secs` 0.2 with Smart Turn and a 3.0 s fallback
([docs](https://docs.pipecat.ai/api-reference/server/utilities/turn-detection/smart-turn-overview));
LiveKit `min_delay` 0.5 / `max_delay` 3.0 s, 0.3 / 2.5 with its detector
([docs](https://docs.livekit.io/reference/agents/turn-handling-options/)). A tuned agent waits
0.2–0.3 s after a complete-sounding sentence and 1–2.5 s after an incomplete one. Barge-in is
Pipecat's default in the streaming pipeline; `MinWordsInterruptionStrategy(min_words=2–3)` keeps
coughs from interrupting ([docs](https://docs.pipecat.ai/server/utilities/interruption-strategies)).
Human turn gaps average ~200 ms ([Stivers 2009](https://pmc.ncbi.nlm.nih.gov/articles/PMC4586330));
the practical voice-agent target is 500–800 ms voice to voice, unreachable with a 1–5 s model, so
hiding the model behind speech matters more than any single swap.

### Text to speech

| Candidate | Size | RTF on ARM CPU | Quality | Licence |
|---|---|---|---|---|
| Piper medium (current) | 61–75 MB | Pi 4: 0.77 (1 thread) → 0.36 (4) ([sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/vits.html)) | adequate, flat prosody | MIT (archived rhasspy) / GPL-3 (piper1-gpl) |
| Kokoro-82M | 310 MB | Pi 4: 7.6 → 3.2; ~1 on a 2-core ARM ([sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html)) | best of the small models; Hindi voices, no Indian English | Apache-2.0 |
| Supertonic 3 | ~99M | no ARM figure | 31 languages | code MIT, weights OpenRAIL-M |
| KittenTTS nano/micro/mini | 15–80M | anecdotal | small-model quality, English | Apache-2.0 |
| Orpheus 3B / Chatterbox | 3B / 0.5B | needs a GPU | excellent | Apache-2.0 / MIT — too heavy here |

### What could run on the Hexagon

Whisper-Small w8a16 and Whisper-Large-V3-Turbo have published IQ-8275 rows on Qualcomm AI Hub;
context binaries are tied to one Hexagon generation (v75 here). Gemma 4 E2B's audio encoder is
already resident through LiteRT-LM ([model card](https://ai.google.dev/gemma/docs/core/model_card_4)).
Nothing for TTS or turn detection is published for Hexagon; Smart Turn is small enough for the CPU.

### Not found measured

ARM CPU latency for Moonshine v2, Supertonic, KittenTTS, distil-small.en; Parakeet TDT's WER on
Indian-accented English; Whisper-Small w8a16's accuracy loss and whether the AI Hub binary exposes the
no-speech and language logits needed for gating; Smart Turn v3 on Indian-accented English; NPU
contention with Whisper, Gemma and YOLO on one HTP.
