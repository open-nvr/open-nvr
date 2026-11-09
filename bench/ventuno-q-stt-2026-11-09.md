# Ventuno Q — speech to text on the NPU vs the CPU (measured 2026-11-09)

Board and runtime as in [`ventuno-q-npu-agent-llm-2026-11-07.md`](ventuno-q-npu-agent-llm-2026-11-07.md):
Arduino Ventuno Q (QCS8275, Hexagon v75, eight ARM cores), the box's own containers, measured at
13:20 UTC with the detector, the NPU captioner and the agent running as deployed. Both adapters are
ai-adapter images on the box; each was called the way the agent calls it (one WAV per request, the
§5.3 result) from a shell on the box, five utterances each, the WAVs made by the agent's own Piper
voice from phrasings in `examples/camera-agent/tools/stt_synthesise.sh` plus three non-speech clips.

- **whisper_qnn** — ai-adapter `adapters/whisper_qnn` (#85): Qualcomm AI Hub's `whisper_small_quantized`
  v0.66.0, w8a16 QNN context binaries for the QCS8275 (encoder 113 MB, decoder 225 MB), onnxruntime-qnn
  2.7.0 (QAIRT 2.51, ORT 1.31.1), log-mel in numpy, English forced, greedy decoding.
- **parakeet** — the shipped default, Parakeet TDT 0.6B v3 int8 through onnx-asr on the CPU.

## Headline

| | whisper_qnn (Hexagon) | parakeet (ARM CPU) |
|---|---|---|
| per utterance, mean of 5 | **361 ms** | 774 ms |
| of which encoder | 115–167 ms | — |
| of which decoder | ~12 ms a token | — |
| WER, 5 clean Piper sentences | 0.03 (digits written as words: "6 pm" → "six pm") | 0.00 |
| no-speech probability, speech | 0.001–0.013 | not available |
| no-speech probability, silence | 0.90 | not available |
| no-speech probability, office noise ×2 | 0.84 / 0.78 | not available |

The one whisper "error" is spelling: it writes "six pm" where Piper was read "6 pm"; the agent's clock
parser takes both. Parakeet transcribed the silence and noise clips as words; whisper's no-speech
probability lets the agent's existing gate (`adapter_clients.py`: a clip whose every segment is above
0.6 is dropped) throw them away, which is what stops the agent answering the room's hum.

## What this decides

`STT_ADAPTER=auto` (the new default in `.env.example`) is whisper_qnn on a Qualcomm board whose NPU
image is available and parakeet everywhere else: 2.1× faster on this box, equal accuracy on clean
speech, and a working silence gate. An explicit `STT_ADAPTER=parakeet` in an existing `.env` is kept
as typed — a box that has it must change the word to `auto` (or `whisper_qnn`) to move.

## Not measured yet

- Accented and far-field speech (the eval set is Piper's voice; a real-voice comparison is the
  remaining caveat before calling the accuracy equal in the field).
- Languages other than English: the NPU adapter forces English; parakeet carries 25 European
  languages and `whisper` (faster-whisper on the CPU) carries Hindi and translation.
- Load: five utterances in sequence, not the agent's bursts; the decoder's per-token cost means a
  long sentence (30 tokens) is ~0.5 s, still under parakeet's mean.

## How it was run

`/tmp/whisper_bench.sh` on the box (a copy in this repo's history is not kept): `docker run --rm -i`
of each adapter image with the box's QNN devices lent as the compose overlay lends them, one request
per WAV, the adapter's own timing fields read from the response. The box was at load 1.5–2 throughout.
