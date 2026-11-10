# Ventuno Q — the 52-case live eval on the CPU brain vs the NPU (measured 2026-11-10)

Board, runtime and harness as in [`ventuno-q-npu-agent-eval-2026-11-08.md`](ventuno-q-npu-agent-eval-2026-11-08.md):
Arduino Ventuno Q (QCS8275, eight ARM cores, Hexagon v75), the agent as deployed (open-nvr main at #852), one
CP Plus camera (an empty office at night), the 52 cases of `examples/camera-agent/tools/eval_cases.yml` through
`tools/box_eval.sh` from the box itself. The CPU run is `box_eval.sh cpu 1 --cpu`: the agent's LLM switched to
granite4:micro on the host's Ollama for the run (`LLM_PROVIDER=ollama`, no pre-warm), everything else as deployed —
the detector and the captioner stayed on the NPU. The NPU rows are run 6 (2026-11-09 17:24 UTC, 52 × 3) and run 7
(2026-11-10 01:43 UTC, 52 × 1) of the same cases with Gemma 4 E2B on the Hexagon through the litertlm adapter.

Why this run: the router changes of 2026-11-09 (#844, #846, #847) were measured on the NPU agent. CPU installs
are the default, so the question was whether the same router carries granite.

## Headline

| | CPU brain (granite4:micro) | NPU brain (Gemma 4 E2B), run 6 | NPU, run 7 |
|---|---|---|---|
| turns right | **50 / 52** | 153 / 156 | 52 / 52 |
| router decided (tier 0) | 31 / 32 | 96 / 96 | 32 / 32 |
| model hinted (tier 1) | 8 / 9 | 24 / 27 | 9 / 9 |
| model alone (tier 2) | 8 / 8 | 24 / 24 | 8 / 8 |
| tier-0 turn, p50 | **16.0 s** | 0.61 s | 0.55 s |
| tier-1 turn, p50 | **195 s** | 3.1 s | 3.0 s |
| tier-2 turn, p50 | **218 s** | 3.2 s | 2.8 s |
| compose step of a routed turn | 5.3–19.4 s | 0.4–0.9 s | 0.4–0.9 s |
| whole run | 71 min for 52 turns | 20 min for 156 turns | 10 min for 52 turns |
| host Ollama during the run | 630 % CPU | — | — |
| cam23 detector mean (NPU) | 22.7 ms, unaffected | 40–57 ms | 40 ms |

The router carries granite: every tool choice the router made was right, and granite got 16 of 17 of its own
right. What granite cannot do on this box is answer in time — a model turn is three to four minutes, because the
~3,000-token prompt is prefilled from scratch each turn on the ARM cores (no prefix cache survived between
requests here), and even the ~200-token compose prompt of a routed turn costs 5–19 s. The NPU is 30–70× faster on
the same questions.

## The two CPU misses, both deterministic, both fixed (#853)

| case | what granite did | fix |
|---|---|---|
| `remove-the-notification-is-not-a-detection` (tier 0) | the router ran `stop_monitor` and the tool answered "Stopped and removed watch #1 'cars'"; granite's compose turned it into "I'm watching for cars on CTPL CP Plus" — the opposite — in 19 s | a routed stop or view switch speaks the tool's own sentence; no compose call at all (`_DIRECT_SPEECH_TOOLS`) |
| `hourly-what-you-see-is-a-scene-report` (tier 1) | `create_report` with `at: "03:00"` and the schema's example "what the cameras saw overnight" as the query, for "every hour tell me what you see" | an interval named in the question beats a clock time the model invented; a query that is a schema example is replaced by the question |

Neither depends on the model, so both runs gain from them.

## Slowest CPU cases (p50 of the turn)

| case | tier | CPU | NPU run 6 |
|---|---|---|---|
| overnight-summary | 2 | 263 s | 3.7 s |
| which-apps | 2 | 254 s | 2.2 s |
| white-van-yesterday | 2 | 253 s | 3.6 s |
| alarm-after-6pm | 1 | 246 s | 3.3 s |
| who-came-this-morning | 2 | 218 s | 3.3 s |
| last-night | 1 | 206 s | 3.1 s |
| monitor-then-stop | 1 | 204 s | 2.6 s |

## What this says for CPU installs

- The 2026-11-08 CPU run (32 cases, before the router work) was 22 of 25 with 52–194 s a model turn and seven
  cases lost to the 30-minute token. This run is 50 of 52 with the token refreshed mid-run (#850) — the router
  moved most turns to tier 0, where the CPU answers in 16 s instead of three minutes.
- A CPU box should run with `LLM_PREWARM=true` (this run had it off, by the script's design, to leave the
  deployed box untouched) and a model whose prompt prefix Ollama can keep warm; the x86 figures in
  `examples/camera-agent/MODELS_AND_LATENCY.md` (20–25 s a turn on four cores) are the realistic CPU floor,
  not this ARM board's.
- On a Qualcomm board the NPU brain is not an optimisation, it is the difference between a conversation and a
  wait: the same 52 questions took 71 minutes on the CPU and 10 on the NPU.

Raw runs: `/tmp/eval-cpu.{json,log}` and `/tmp/eval-npu.{json,log}` on the box at the time of writing.
