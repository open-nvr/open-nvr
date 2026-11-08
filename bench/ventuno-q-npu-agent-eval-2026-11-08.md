# Ventuno Q — the camera agent on the NPU vs the CPU, the 32-case live eval (measured 2026-11-08)

Board, runtime and bundle as in [`ventuno-q-npu-agent-llm-2026-11-07.md`](ventuno-q-npu-agent-llm-2026-11-07.md):
Arduino Ventuno Q (QCS8275, Hexagon v75), Gemma 4 E2B compiled for the NPU, served to the agent by the
litertlm adapter's `/v1/chat/completions` (ai-adapter #83/#84) with the agent's per-question tool shortlist
inside the 4,096-token window (#817). The CPU side is the shipped default, granite4:micro through the host
Ollama on the eight ARM cores. Same box, same agent image (open-nvr main at #820), same camera (one
CP Plus, 1080p, an empty office between 01:58 and 03:25 UTC), same 32 cases
(`examples/camera-agent/tools/eval_cases.yml`, #818) through `tools/eval_harness.py --throwaway`, run from
inside the agent container against the agent over loopback. The harness arms real alarms and monitors and
removes them; the office was empty, so every live look saw a chair and a table.

## Headline

| | NPU (Gemma 4 E2B) | CPU (granite4:micro) |
|---|---|---|
| cases × repeats | 32 × 3 | 32 × 1 (25 completed, 7 errored — see below) |
| turns right | **85 / 96** | 22 / 25 |
| router decided (tier 0) | 39 / 39 | 11 / 11 |
| model alone (tier 2) | 43 / 54 | 11 / 14 |
| model-alone turn, p50 | **2.0–3.8 s** | 52–194 s |
| live look (tier 0: detect + compose) | 6.1–9.7 s | 16–49 s |
| whole run | 20 min | 33 min for 25 cases |
| box load during the run | under 2 | 8.8–10.4 |

The seven CPU cases that errored did so because the harness's login token (a 30-minute OpenNVR access
token) expired at minute 30 of a 33-minute run: `/reset answered 401`. The agent answered every case it
was asked. The NPU run finished its 96 turns in 20 minutes on the same token.

## Per case

`pass` is the agent doing the right thing (tool, arguments, what it created); `model` is the model
choosing it unprompted (a router-decided turn has no model column). p50 is the whole turn as the
user waits for it, tools included.

| case | tier | NPU pass | NPU p50 | CPU pass | CPU p50 |
|---|---|---|---|---|---|
| alarm-after-6pm | 2 | 3/3 | 3.0 s | 1/1 | 97 s |
| monitor-crowd | 2 | 3/3 | 2.8 s | 1/1 | 79 s |
| overnight-summary | 2 | 3/3 | 3.5 s | **0/1** (answered from a live frame) | 63 s |
| what-is-person-wearing | 0 | 3/3 | 9.0 s | 1/1 | 49 s |
| polite-close | 0 | 3/3 | 0.5 s | 1/1 | 34 s |
| greeting-no-tool | 0 | 3/3 | 1.4 s | 1/1 | 27 s |
| is-anyone-there | 0 | 3/3 | 6.3 s | 1/1 | 19 s |
| past-tense-goes-to-history | 0 | 3/3 | 0.7 s | 1/1 | 8.5 s |
| follow-up-alarm | 2 | 2/3 (an ISO timestamp as `after`) | 9.4 s | 1/1 | 103 s |
| count-people-live | 0 | 3/3 | 6.4 s | 1/1 | 39 s |
| any-vehicles-live | 0 | 3/3 | 6.6 s | 1/1 | 16 s |
| generic-describe | 0 | 3/3 | 7.5 s | 1/1 | 21 s |
| what-is-happening | 0 | 3/3 | 9.7 s | 1/1 | 49 s |
| is-the-gate-clear | 2 | 3/3 | 9.7 s | 1/1 | 69 s |
| anyone-anywhere | 0 | 3/3 | 6.3 s | 1/1 | 17 s |
| white-van-yesterday | 2 | 3/3 | 3.4 s | 1/1 | 70 s |
| last-night | 2 | 3/3 | 3.8 s | 1/1 | 194 s |
| last-ten-minutes | 2 | 3/3 | 2.4 s | 1/1 | 52 s |
| between-two-and-three | 2 | 3/3 | 3.8 s | 1/1 | 155 s |
| who-came-this-morning | 2 | 3/3 | 3.1 s | 1/1 | 93 s |
| delivery-today | 0 | 3/3 | 0.6 s | 1/1 | 10 s |
| notify-car-arrives | 2 | **0/3** (create_alarm, "after midnight") | 3.0 s | 1/1 | 129 s |
| monitor-more-than-two | 2 | 3/3 | 2.7 s | **0/1** (counted once, no monitor) | 127 s |
| alarm-before-6am | 2 | **0/3** (`after` 06:00) | 3.2 s | **0/1** | 90 s |
| alarm-silent | 2 | 2/3 (an ISO timestamp as `after`) | 3.2 s | 1/1 | 174 s |
| alarm-then-stop | 2 | 3/3 | 5.1 s | errored (token) | — |
| monitor-then-stop | 2 | **0/3** (an alarm, then stop_alarm) | 5.1 s | errored (token) | — |
| which-apps | 2 | 3/3 | 2.0 s | errored (token) | — |
| plate-alerts | 2 | 3/3 | 2.9 s | errored (token) | — |
| how-many-cameras | — | 3/3 | 7 ms | errored (token) | — |
| good-morning | 0 | 3/3 | 1.0 s | errored (token) | — |
| follow-up-same-camera | 0 | 3/3 | 12.6 s | errored (token) | — |

## What the misses were, and what was done

**NPU, 11 of 96.** Two causes, both deterministic, fixed in open-nvr #821:

- Six: "let me know when a car arrives" and "notify me when a person shows up" went to `create_alarm`,
  each time with an invented *after midnight* window (and the stop follow-up then stopped an alarm);
  "notify me when more than 3 people gather" went to `create_monitor` every time. The router now hints a
  standing request by its words (alarm / siren / ring / loud / wake me → `create_alarm`; notify / let me
  know / tell me when / watch / keep an eye → `create_monitor`; a stop word → the matching stop tool).
- Five: "before 6am" arrived as `after='06:00'` three times of three, and two windows arrived as full
  timestamps (`2026-11-08T22:00:00+0000`) the agent rejected into an all-day alarm. The alarm handler
  now takes the window from the question's own clock words and drops a window the question never named;
  a timestamp is read as its time of day.

**CPU, 3 of 25.** Different misses: "what did the cameras see overnight?" answered from a live frame
(no history tool called), "tell me if more than 2 people" counted once instead of arming a monitor, and
the same `before 6am` confusion. The first two are the small-CPU-model behaviour the router and the
forced grounding exist for (MODELS_AND_LATENCY.md); the third is fixed by #821 on both.

## Where the time goes now

On the NPU the model is 0.5–0.9 s to compose an answer and 2–4 s to choose and fill a tool. The live
looks (6–10 s) are the agent opening its own RTSP session for a fresh frame when no Tier-0 track is
active: the system check measures it at 6.2 s against 0.1 s for the detector. Open-nvr #822 makes the
pipeline serve the frame it just decoded (`/latest_frame`) and the agent read that first; expected live
look about 0.7 s for a count and 2.5 s for a description. To be re-measured.

## The third run, after the fixes (04:25 UTC)

Same box, same cases, the agent image with #821, #822 and #824 (the second run, at 03:53, had #821
and #822 only: 90 of 96, live looks still 5.8 s — the agent was resolving the camera to its bare
server id "23" for the pipeline's endpoints while the pipeline knows it as `cam23`, so every
best-frame and latest-frame fetch was a 404; #824).

| | run 1 (01:58) | run 3 (04:25) |
|---|---|---|
| turns right | 85 / 96 | **96 / 96** |
| router decided / model hinted / model alone | 39/39 · — · 43/54 | 39/39 · 27/27 · 27/27 |
| live look: detect_objects tool | 5.6–5.8 s | **0.11 s** (median; max 0.17) |
| live look: describe_camera tool | 8.0 s | **2.2 s** (median; max 3.0) |
| whole turn, median / p90 / max | — | 2.7 s / 4.0 s / 5.7 s |
| LLM step (choose a tool, or compose), median / p90 | — | 1.2 s / 1.9 s |
| the 96 turns | 20 min | **3.8 min** of agent time |

Per case, the turn the user waits for (p50): the router-decided live looks 0.55–0.8 s (is anyone
there, how many, any cars, anyone anywhere), descriptions 1.9–3.9 s, history questions 2.3–3.8 s,
alarms and monitors 2.6–3.6 s, the two-turn stop follow-ups 4.6–5.3 s, the roster question 9 ms.

## Not measured here

- Detector latency while the model answered: the room was empty and the detector ran few inferences
  (mean 42.7 ms over the run, no worse than idle); a walk with people in frame is the real test.
- Answer quality beyond the tool choice and the stored state (the replies were read, not scored).

## Reproduce

On the box: `bash /tmp/run_eval.sh <label> <repeat>` (prompts for an OpenNVR login, runs the harness
inside the agent container against `https://127.0.0.1:9100`, camera `cam23`, `--throwaway`); the two
raw runs are `/tmp/eval-npu.json` and `/tmp/eval-cpu.json` there. For the CPU side the agent was
switched to `LLM_PROVIDER=ollama` with the host Ollama and `granite4:micro`, then restored. A run longer
than 30 minutes needs a longer-lived token than a login grants.
