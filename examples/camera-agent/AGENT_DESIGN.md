<!--
Copyright (c) 2026 OpenNVR
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# OpenNVR Agent (formerly Camera Agent) design: tools vs prompt vs flows, and how to keep it good

A practical guide to *where* each capability should live and *how* to measure
"better". The rule of thumb:

> **Capability → a tool. Behavior/policy → the prompt. A reusable multi-step
> capability you want modular + testable → a flow/skill.** And what actually
> makes the agent better over time → **evals.**

## The layers (and why)

- **Tools (the backbone).** The agent is capable because it has good
  function-call tools — `detect_objects`, `describe_camera`, `create_alarm`,
  `recent_events`, `search_footage`. The LLM only *picks* a tool and *phrases*
  the result; the tool does the real work and forces the answer to come from
  reality. Never make the model "do vision" or "remember the past" in the prompt
  — give it a tool. Fewer, well-scoped tools also mean small local models pick
  the *right* one (why `enabled_tools` matters).
- **Prompt (thin policy layer).** Identity, hard rules ("never guess — call a
  tool first"), and output style ("spoken, locations not ids, 1–2 sentences").
  Keep it short — long prompts make small models miss tool calls.
- **Flows / skills (modularity at scale).** A *flow* (e.g. Pipecat Flows) or
  *skill* (instructions + its own tools, loaded on demand) earns its keep for
  **multi-step, reusable** capabilities — so the base prompt stays lean, each
  capability is independently testable, and it can be shared across a *family*
  of agents (doorbell, drone, robot).

## Capability map (current → where it should live)

| Capability | Today | Best home | Why |
|------------|-------|-----------|-----|
| Detect / count objects | tool | **stay a tool** | single-shot, well-scoped |
| Describe / VQA a frame | tool | **stay a tool** | single-shot |
| Recent-events lookup | tool | **stay a tool** | single query |
| Standing monitor (notify/count) | tool | **stay a tool** — converged onto the App SDK¹ | one call sets it up |
| **Alarm setup** (target + time window + actions) | tool | **→ a FLOW** | multi-slot: target, window, camera(s), emergency contact — a guided dialogue is clearer than one big tool call |
| **Footage search** (NL over the past) | tool/example | **→ a FLOW** | iterative: clarify time range → search → refine → present; benefits from back-and-forth |
| **Watchlist / face enrollment** | tools | **→ a FLOW/skill** | multi-step (capture → name → confirm → store), reusable across agents, easy to get wrong without structure |

¹ The count/crossing kinds run the App SDK rule classes in-process via
`monitor_host.MonitorHost` (spec §07, "one rule library, two front doors" —
see [`TWO_DOORS.md`](../../docs/TWO_DOORS.md)); the notify kind keeps the
legacy cooldown-refire loop.

Everything else is best as a plain tool. The three bolded ones are the
candidates to turn into **Pipecat Flows** as the agent matures — not now, but
when their prompt guidance starts bloating the base prompt.

## Measuring "better" — the eval strategy

Two tiers, both seeded:

1. **Deterministic evals (CI, no LLM)** — `tests/test_agent_evals.py`. A growing
   matrix of representative phrasings → expected tool routing / grounding gate /
   noise handling / end-to-end synthetic answers. This already caught real
   routing gaps (plurals like "dogs", missing "gate"/"wearing" → the latter was
   a fabrication risk). **Add a row here every time you find a mis-handled
   phrasing** — it's the agent's regression net.
2. **Live evals (need a model)** — extend `tools/latency_harness.py` into a
   correctness harness that runs against a real agent and checks the *answer*,
   not just latency. These cover the LLM-dependent behaviors the CI evals can't:

   | Query | Expected behavior |
   |-------|-------------------|
   | "set an alarm if a person is seen after 6pm" | calls `create_alarm` with target=person, after=18:00 |
   | "notify me when more than 3 people gather" | calls `create_monitor` kind=count |
   | "what did the cameras see overnight?" | calls `create_background_task` / `recent_events`, doesn't fabricate |
   | "what is the person wearing?" | calls `describe_camera` (VQA), gives an attribute answer |
   | "thanks, that's all" | no tool call, polite close |

   Run these against each candidate model (qwen2.5:1.5b, qwen2.5:0.5b, a cloud model)
   to pick the brain per deployment. **This is `tools/eval_harness.py`**, with
   the cases above (and more) in `tools/eval_cases.yml`:

       python tools/eval_harness.py --url http://localhost:9100 \
           --camera <id> --repeat 5 --throwaway --out run.json

   Scoring is deterministic: the turn trace says which tool ran, who chose it
   (`by`: model / router / forced) and the arguments as asked, and the harness
   reads the alarms and monitors a case created back from the agent, so both
   what the model asked for and what the agent made of it are checked. Each
   case reports a **pass** rate (the agent did the right thing) and a
   **model** rate (the model chose it itself — a forced grounding does not
   count), with a 95% interval over the repeats, grouped by router tier.
   For the model on its own, run the agent with `router_tier0: false` and
   `router_hints: false`; the JSON records which way it ran. Only against a
   throwaway agent: cases arm real alarms and clear the chat history.

## The takeaway
Don't reach for prompt tricks. Make capabilities **tools**, keep the **prompt**
a short policy, promote the few multi-step ones to **flows** when they grow, and
let **evals** — not vibes — tell you whether a change helped.
