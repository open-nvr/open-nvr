# OpenNVR Agent — user guide

Everything the agent can do, how each capability is enabled, and how to
use it. This is the operator-facing companion to
[AGENT_DESIGN.md](AGENT_DESIGN.md) (architecture) and the deep-dives it
links. If a term appears in the demo UI, it is explained here.

The agent runs in two flavours from the same code — **voice**
(`--profile camera-agent`: speak, hear the answer) and **chat**
(`--profile camera-agent-chat`: type, read). Both expose the demo at
`https://localhost:9100/demo`. Everything below applies to both unless
stated otherwise.

---

## Skills

A **skill** is a user-facing capability the agent carries — "see what's
happening", "count people", "search recorded footage". Each skill maps
to one or more **tools** (the functions the LLM can call). Switching a
skill off drops its tools from the advertised set, so the model can no
longer call them — the agent genuinely reconfigures, it doesn't just
refuse politely.

| Skill | Ask it… | Voice default | Chat default | Needs |
|---|---|---|---|---|
| **see** | "What's happening at the front door?" | ✅ on | ✅ on | nothing (falls back to the object detector until a caption/VQA adapter registers) |
| **count** | "How many people are in the back yard?" | ✅ on | ✅ on | nothing (standard YOLOv8) |
| **footage** | "Did a red truck come by earlier today?" | ✅ on | ✅ on | the footage-search app (on by default) |
| **apps** | "Is the occupancy counter healthy?" | ✅ on | ✅ on | nothing (reads the app registry) |
| **events** | "Did anyone come to the door in the last 30 minutes?" | ✅ on | ✅ on | nothing (the NATS event bus) |
| **alarm** | "Alarm me if someone is at the door after 10pm." | ✅ on | ✅ on | nothing |
| **watch** | "Watch the driveway and tell me if more than 3 cars show up." | ✅ on | ✅ on | nothing |
| **faces** | "Who's at the front door?" | ⬜ off | ⬜ off | the InsightFace recognition adapter |
| **report** | "Every morning at 7, summarise overnight activity." | ⬜ off | ⬜ off | nothing — enable in config (below) |
| **task** | "Check every camera for anyone in a red shirt." | ⬜ off | ⬜ off | nothing — enable in config (below) |

**How enablement works — two layers.**

1. **The config allowlist** (`enabled_tools` in the agent's config)
   decides which tools are *ever* advertised. The docker demos ship a
   deliberate list (the ✅ column above) to keep the LLM prompt short —
   on CPU, every extra tool definition slows the first token. A
   bare-metal config with `enabled_tools` unset advertises everything.
   To turn a ⬜ skill on, add its tool names to the list, e.g. for
   background tasks and reports:

   ```yaml
   enabled_tools:
     # …existing entries…
     - create_background_task        # the "task" skill
     - create_report                 # the "report" skill
     - stop_report
   ```

2. **The runtime toggle** (the demo's **Skills** card, admin tier)
   switches a skill on/off live without a restart. A skill whose
   backend isn't wired is greyed out **with a reason and a deep link**
   — e.g. faces shows "needs a face-recognition adapter" and links to
   the AI Adapters page to register one. The greyed state is honest:
   it reflects what is actually registered in KAI-C right now.

## Alarms

Alarms are the **urgent** tier: when their target appears on a watched
camera (optionally only within a time window), they **ring in the UI
until a human acknowledges them**. Full mechanics: [ALARMS.md](ALARMS.md).

**Creating one — three ways:**

* **By voice/chat**: "Sound a fire alarm if you see fire", "Alarm if a
  person is detected after 6pm", "Alert me loudly if a car enters
  between 10pm and 6am on all cameras". Overnight windows
  (`22:00`–`06:00`) wrap correctly.
* **One-click presets** on the Alarms card. Presets are
  **capability-aware**: the server checks each preset's target against
  what the detection path can actually see. Presets the stock stack
  can serve — *After-hours person, Person, Car, Truck, Dog* — are
  armable immediately. The safety presets — *Fire, Smoke, Gas leak* —
  are **greyed out on a stock install**, because the standard
  YOLOv8/COCO detector has no fire, smoke, or gas class; clicking one
  explains exactly what to do (register a detection adapter trained
  for it, then list its label under `detector_extra_labels` in the
  agent config, which lights the preset up). A Fire button that armed
  an alarm which could never ring would be worse than none.
* **The + form** on the Alarms card: free-form name, target, alert
  level, time window.

**Alert levels** (per alarm, preselected by target):

| Level | Behaviour |
|---|---|
| **siren** (critical) | latches — rings until a human silences it |
| **pulse** (urgent) | loud for a minute, then stands down on its own |
| **chime** | a single ding, no latch |
| **silent** | notifications only |

Fire-grade targets (fire, smoke, flame, gas) default to **siren**;
everything else to **chime**. The ⚙ button on the Alarms card edits
this **site policy** (admin tier) — a farm can make "snake" critical, a
bank "person". Nothing is pre-armed out of the box: arming is always a
one-click or one-utterance human decision, and only **operator-tier**
users can arm or disarm anything (see Access tiers).

If an `emergency_contacts` entry matches the alarm, the triggered event
is tagged with the contact so your notification channel can escalate —
the agent itself never dials anyone.

## Watches (monitors)

The **informational** tier — same detection engine as alarms, calmer
output: "watch the driveway and tell me if more than 3 cars show up"
creates a monitor that counts over time and posts a notification when
the condition is met, without ringing anything. Use an alarm when a
human must react *now*; a watch when you want to *know*. Watches live
in the right-rail "Watches" card; stop one by voice ("stop watching the
driveway") or with its ✕.

## Background tasks

A **task** is a one-shot, longer-running job that would block the
conversation if run inline: "check every camera for anyone in a red
shirt" fans out over every camera, runs detection and description on
each, and reports back when done — while you keep talking.

* **How it works**: the agent queues the job and answers immediately;
  a background worker runs a full tool-calling turn for the query and
  stores the result. The Tasks card polls progress, and the completion
  is surfaced back into the chat. Guardrail: a background task runs
  with the agent-control tools removed — a task can't spawn more
  tasks, arm alarms, or create watches.
* **How to enable**: off in both docker demos (it's the most
  token-hungry skill on CPU). Add `create_background_task` to
  `enabled_tools` and restart, or run a bare-metal config with
  `enabled_tools` unset.
* **Task vs watch**: a task runs once and finishes; a watch runs
  forever until stopped.

## Events

The agent remembers what the detection stream saw. With the **events**
skill on (default), ask about the recent past — "did anyone come to the
door in the last 30 minutes?" — and the agent answers from the NATS
event ring (`recent_events`). This is short-term memory (the in-process
ring, `event_ring_size`); for days-back questions use **footage**
search, which reads the persistent index. The skill's deeper tools —
`search_history`, `describe_event`, `describe_window` — are not in the
docker allowlists (prompt size); add them to `enabled_tools` to let the
agent narrate a specific event or time window.

## Alerts & notifications

Three streams end up in front of you:

* **The notification feed** the demo polls: alarm rings, watch hits,
  task completions — and **app alerts**. The agent subscribes to
  `opennvr.alerts.app.>` on the bus, so any catalog app's alert (on a
  stock install: the default-on occupancy counter's *over-occupancy*)
  lands in the feed proactively, throttled per app+camera so a
  misbehaving app can't spam. Ask "any occupancy alerts this morning?"
  (`recent_app_alerts`) for the full, unthrottled history.
* **The events card**: the same items, in the right rail.
* **External delivery**: webhooks / Apprise push (ntfy, Telegram,
  email, …) via `notify_webhooks` / `notify_apprise` — see
  [NOTIFICATIONS.md](NOTIFICATIONS.md). Off until configured; the
  "Test" button verifies a channel end-to-end.

## Footage search

"Did a red truck come by earlier today?" — the **footage** skill
searches the index that the footage-search app (on by default) builds
from the inference stream: object labels from the detector, scene
captions when a captioner runs. The agent decomposes your question into
keywords, time window, and camera; results come back newest-first as
distinct sightings. The index lives on the footage-search volume
(mounted read-only into the agent); if the indexer hasn't created it
yet the tool says so and lights up by itself once it exists — no
restart needed.

## Apps door

With the **apps** skill (default on), the agent reads the OpenNVR app
registry: "what apps are running?", "is the occupancy counter
healthy?", "any alerts from the loitering detector?". Strictly
**read-only** — the agent reports on apps; enabling, disabling, or
configuring one stays in the OpenNVR App Catalog UI.

## Faces

Off by default — it needs the InsightFace recognition adapter, which
the standard stack doesn't register. Once the adapter is up (AI
Adapters page), the faces skill un-greys and the agent can recognise
("who's at the door?"), enroll ("remember this person as Sam"), list,
and forget people. Details: [FACES.md](FACES.md).

## Reports

"Every morning at 7, summarise overnight activity" — a scheduled query
whose answer lands in the Reports card and the notification feed. Off
in both docker demos; enable by adding `create_report` / `stop_report`
to `enabled_tools`.

## Access tiers

When the agent delegates auth to OpenNVR (`auth_mode: opennvr`, the
docker default), your OpenNVR role maps to a tier:

| Tier | Can |
|---|---|
| **viewer** | look and chat — every read tool, plus the read-only app door and face listing |
| **operator** | everything viewers can, **plus** arm/disarm alarms, create/stop watches, tasks, reports |
| **admin** | everything, **plus** toggle skills and edit the site alert-level defaults |

The tiers are enforced at the toolset, not just the UI — a viewer's
chat physically lacks the mutating tools, so it can't be talked into
arming anything.

## Wake word

**Off by default, on purpose.** In voice mode the agent listens only
while a session is open (click **Talk**); **Stop** always works — even
mid-thought, it cancels the in-flight turn server-side. The optional
wake word can be enabled per-session in the demo, but with CPU
transcription accuracy a mis-heard wake word eats real questions, so
the default stays off.

---

## Quick reference — what a stock install gives you

Say it and it works, no configuration:

* "What's happening at the front door?" · "How many people are in the
  back yard?" (see / count)
* "Did a red truck come by earlier today?" (footage)
* "Did anyone come to the door in the last 30 minutes?" (events)
* "Alarm me if someone is at the door after 10pm." · "Stop alarm 2." (alarm)
* "Watch the driveway and tell me if more than 3 cars show up." (watch)
* "Is the occupancy counter healthy?" · "Any occupancy alerts this
  morning?" (apps)

One config line away: background tasks, reports. One adapter away:
faces, and the Fire / Smoke / Gas-leak alarm presets.
