# Ventuno Q — Gemma 4 E2B on the Hexagon NPU as the camera agent's LLM (spike, measured 2026-11-07)

Board and runtime as in [`ventuno-q-npu-captioner-2026-11-05.md`](ventuno-q-npu-captioner-2026-11-05.md):
Arduino Ventuno Q (QCS8275, Hexagon v75), LiteRT-LM 0.18.0 with the NPU build of the native library, the
bundle `gemma-4-E2B-it_Qualcomm_QCS8275.litertlm` (2.72 GB, compiled with a 4,096-token context). The spike
ran inside the captioner's container as a second engine on the NPU, while the captioner, the detector and the
recorder ran as usual. Every number is wall-clock on the box; the runtime's token counter
(`get_benchmark_info`) fails on this build, so token counts are estimates from characters.

## Why

The camera agent's LLM runs on the ARM cores through Ollama (granite4:micro): 20–25 s a turn, a 288 s
re-prefill when the cached prefix changes, and a 3 m 50 s warm-up at every restart. The captioner already
proved the same Gemma bundle on the NPU at 1.7 s a photo. The question: can it be the agent's LLM — speed,
tool calling, and whether the agent's prompt fits the fixed window.

## Input

The agent's REAL prompt, dumped from the running camera agent on the box: a 3,282-character system prompt
(identity, camera roster, spoken-style rules, routing paragraphs) and the 14 tool schemas it advertises on
this box, 12,777 characters of JSON (describe_camera, detect_objects, search_footage, search_history,
describe_event, describe_window, recent_events, create_monitor, stop_monitor, create_alarm, stop_alarm,
list_apps, app_status, recent_app_alerts). Roughly 800 tokens of system text and 3,200 of schemas.

## Tool calling

LiteRT-LM's Python API takes tools natively: each tool is an object whose description is the OpenAI
envelope `{"type": "function", "function": {...}}`, the agent's own schema unchanged. With
`automatic_tool_calling=False` the response carries `tool_calls=[ToolCall(name, arguments)]`, structured, no
text parsing. Ten questions from the agent's evaluation cases, seven tool schemas attached, thinking off,
96–128 output tokens:

| question | expected | Gemma called | s |
|---|---|---|---|
| set an alarm if a person is seen on the front door camera after 6pm | create_alarm | create_alarm | 1.6 |
| notify me when more than 3 people gather on the front door camera | create_monitor | create_monitor | 1.4 |
| what did the cameras see overnight? | any history tool | recent_events | 1.2 |
| what is the person on the front door camera wearing? | describe_camera | describe_camera | 1.4 |
| thanks, that's all | no tool | none | 1.1 |
| hi, what can you do? | no tool | none | 1.4 |
| is anyone on the front door camera right now? | detect_objects or describe_camera | detect_objects | 1.2 |
| did anyone come by the front door camera this morning? | any history tool | recent_events | 1.2 |
| did a white van come to the gate yesterday? | search_history or search_footage | search_history | 2.0 |
| which apps are running? | list_apps or app_status | list_apps | 0.8 |

**10 of 10, twice over (two separate runs), 0.8–2.0 s a turn end to end.** The arguments were right too
(`{"camera_id": "cam23"}`; the describe call carried the question). On the CPU the same model class rarely
calls a tool unprompted and the agent's router and forced grounding do the work (MODELS_AND_LATENCY.md).

## The window

| prompt | result |
|---|---|
| system + 2 tools | 1.1 s |
| system + 7 tools | 0.8–3.4 s (first call after load 3.4 s) |
| system + 9, 11, 12, 13 tools | 1.1–1.8 s |
| system + all 14 tools | refused: `litert_lm_conversation_send_message failed` (the window) |
| system + 7 tools + 4 turns of history | 1.0 s |
| system + 7 tools + 8 turns (16 messages, ~1,200 tokens) | 1.0 s |
| system + 7 tools + 16 turns | refused |

So the 4,096-token bundle holds the system prompt, thirteen schemas and a short history, or seven schemas
and eight turns. The agent's shipped configuration (all tools, eight messages of history, 8,192-token Ollama
window) does not fit as it stands; the plan's shortlist (the router picks the tool, the model sees two to
seven schemas) and a token budget for history make it fit with room.

## Memory and contention

The second engine: 1.5 GB resident, 114 MiB of it heap, the rest the mapped model file (reclaimable). The
detector's latency while the model answered was not measured: the room was empty throughout, so the detector
ran no inferences. To be repeated during a walk, together with a caption requested mid-answer.

## Not measured

- Output quality against granite on open questions (only tool choice and the short answers were read).
- Thinking mode (off throughout).
- Sustained turn rate and the NPU shared three ways (detector, captioner, agent).

## Reproduce

`scripts` in the scratchpad of the session, to be committed with the adapter's chat endpoint: dump
`build_system_prompt()` and `tool_definitions` from the running agent; inside the litertlm-adapter
container, `LitertLmService(fetch_libraries=False).load_now()`, then `engine.create_conversation(system_message=…,
tools=[…], automatic_tool_calling=False, max_output_tokens=96, thinking_config=ThinkingConfig(enable_thinking=False,
thinking_token_budget=0))` and `send_message(question)`.
