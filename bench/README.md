# bench — measuring whether the tier-0 architecture earns its keep

Two numbers settle the "is tier-0 + event memory worth it, or are we losing
data" question instead of guessing.

## 1. `tier0_recall.py` — what the cheap detector SEES

Recall bounds everything downstream: a person tier-0 misses is an event never
remembered and an alert that never fires. Point it at a detector's `/infer`
endpoint and a labelled image manifest; it reports presence recall/precision
per label, **broken down by condition** (static / edge / close / low-light) so
the hard cases aren't hidden by the easy-case average.

```bash
python bench/tier0_recall.py --url http://localhost:9108/infer --manifest labels.json
```

Manifest: `{"images":[{"path":"...","truth":{"person":1},"condition":"static"}, ...]}`
(`truth` = ground-truth presence counts; 0 = absent).

**Read it as:** low recall on `static`/`close`/`edge` is the exact blind spot
behind "the agent didn't see the person sitting there." Track it over time; it
is what the automatic index costs you.

## 2. `event_memory_dividend.py` — what the store is WORTH

A look-only agent sees nothing between questions. Every stored event is a
moment it would have missed. This queries the events API over a window and
reports the dividend by label and hour, calling out the **unattended hours**
(overnight/quiet) — the clearest "would have been lost without the store" set.

```bash
python bench/event_memory_dividend.py --url http://localhost:8000 --token "$JWT" --days 7
```

**Read it as:** a large unattended count means the memory is doing real work a
query-only agent structurally cannot.

## The verdict these produce

- Good recall + meaningful memory dividend → the architecture earns its keep;
  keep it, and hold consumers to the liveness rule (cheap layer never
  substitutes for a live look on a "now" question).
- Poor recall or ~nothing consuming the stream → that's the QA "no observable
  difference"; harden recall or simplify — but now you *know*, from evidence.

The scoring/summary logic is stdlib-only and unit-tested (`test_bench.py`); the
network parts need a running detector / backend.

## Measured on hardware

Reports taken on real boards, every number from a run on that board:

- [`ventuno-q-benchmark-2026-10-30.md`](ventuno-q-benchmark-2026-10-30.md) — the Tier-0 detector on the
  Hexagon NPU of an Arduino Ventuno Q (QCS8275) against the CPU: 8.6 ms an inference on the HTP, sixteen
  cameras clean where the CPU managed four.
- [`ventuno-q-npu-captioner-2026-11-05.md`](ventuno-q-npu-captioner-2026-11-05.md) — Gemma 4 E2B as the
  caption / visual-question model on the same NPU through LiteRT-LM: 1.7 s a photo where a CPU vision model
  took a minute, with the detector sharing the NPU.
- [`ventuno-q-npu-agent-llm-2026-11-07.md`](ventuno-q-npu-agent-llm-2026-11-07.md) — the same Gemma bundle as
  the camera agent's LLM: native tool calling, 10 of 10 tool choices right on the agent's evaluation questions
  at 0.8–2.0 s a turn (20–25 s on the CPU), and the exact fit of the 4,096-token window (13 of 14 schemas, or
  7 schemas plus eight turns of history).
- [`ventuno-q-npu-agent-eval-2026-11-08.md`](ventuno-q-npu-agent-eval-2026-11-08.md) — the agent's 32-case
  live eval on the NPU against the CPU default, same box, same cases: 85 of 96 turns right at 2–4 s a
  model turn (22 of 25 at 52–194 s on the CPU), the eleven misses traced to two deterministic fixes, the
  live look's 6 s found to be the RTSP grab, not the model — and the third run after the fixes: 96 of 96,
  live looks 0.6–0.8 s, the whole 96 turns in under four minutes.
- [`ventuno-q-stt-2026-11-09.md`](ventuno-q-stt-2026-11-09.md) — speech to text on the Hexagon
  (Whisper-Small w8a16, `whisper_qnn`) against parakeet on the ARM cores: 361 ms vs 774 ms an
  utterance, equal clean-speech accuracy, a working no-speech gate; why `STT_ADAPTER=auto` picks it.
