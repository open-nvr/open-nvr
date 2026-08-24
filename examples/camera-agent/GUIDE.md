# OpenNVR Agent — user guide

Everything the agent can do, how each capability is enabled, and how to
use it. This is the operator-facing companion to
[AGENT_DESIGN.md](AGENT_DESIGN.md) (architecture) and the deep-dives it
links. If a term appears in the demo UI, it is explained here.

The agent runs in two flavours from the same code — **voice**
(`--profile camera-agent`) and **chat** (`--profile camera-agent-chat`).
Both expose the demo at `https://localhost:9100/demo`, laid out
chat-first: the conversation fills the page and the input bar is docked
at the bottom. On a voice install that bar carries a **dictation mic**
(speak, and the words land in the text box to edit and send) and a
compact **Talk** button for hands-free sessions (it listens and answers
aloud until you tap Stop or it idles out); a chat install ships no
STT/TTS, so it shows the text box alone. On phones a bottom tab bar
shows one section at a time — Chat · Activity · History · Automations ·
Skills. Everything below applies to both flavours unless stated
otherwise.

---

## Skills and tasks — the two words that matter

Everything the agent does is built from two ideas. Get these two and
the rest of this guide is detail.

A **skill** is a capability the agent *has* — something it knows how to
do. Seeing and analysing a live stream is a skill. Counting objects is
a skill. Searching recorded footage is a skill. Recognising faces is a
skill. Skills are the agent's verbs, and the set is designed to grow:
if a *calling* skill were added tomorrow (dial a VoIP/SIP number),
nothing else about the agent would change — there would simply be one
more thing it knows how to do, and every assignment built on it would
light up.

A **task** is an assignment *you* give, built out of those skills.
"Send me a report at 8 AM every day of how many trucks entered" is a
task: the agent takes it and keeps doing it — the counting skill plus a
schedule. "If someone comes to the office gate after 6 pm, call me on
my SIP URI" would be a task too: the detection skill composed with that
future calling skill, standing until you cancel it.

So: **skills are what the agent can do; tasks are what you've asked it
to keep doing with them.** Adding a skill upgrades what every future
task can be made of; adding a task never requires new code — it's just
you talking.

---

## Skills

A skill maps to one or more **tools** (the functions the LLM can
call). Switching a skill off drops its tools from the advertised set,
so the model can no longer call them — the agent genuinely
reconfigures, it doesn't just refuse politely.

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

## Tasks — the assignments you can give

Every assignment the agent accepts is one of four shapes. They differ
on just two axes: **when it runs** (once, continuously, or on a clock)
and **how loudly the outcome arrives** (an answer, a notification, or a
latching ring):

| Shape | Runs | Outcome | Example | Tool |
|---|---|---|---|---|
| **Background run** | once, then done | answer back in chat | "Check every camera for anyone in a red shirt." | `create_background_task` |
| **Watch** | continuously until stopped | notification when the condition is met | "Watch the driveway and tell me if more than 3 cars show up." | `create_monitor` |
| **Alarm** | continuously until stopped | **rings until a human acknowledges** | "Alarm me if someone is at the door after 10 pm." | `create_alarm` |
| **Report** | on a schedule, forever | a delivered summary, each time | "Every day at 8 AM, tell me how many trucks entered." | `create_report` |

Why is a watch not just a task? Because "once" and "forever" are
different promises: a background run finishes and is gone; a watch is a
*standing* assignment that keeps consuming the stream until you cancel
it. And why does the alarm get its own name when it's shaped like a
watch? **Urgency.** A watch informs — read it when you like. An alarm
interrupts — it latches and rings until a person deals with it. Same
engine, opposite contract with your attention. The four shapes are one
family; the names just tell you which promise you're getting.

Each shape is detailed in its own section below.

## Alarms

Alarms are the **urgent** tier: when their target appears on a watched
camera (optionally only within a time window), they **ring in the UI
until a human acknowledges them**. Full mechanics: [ALARMS.md](ALARMS.md).

**Creating one — three ways:**

* **By voice/chat**: "Sound a fire alarm if you see fire", "Alarm if a
  person is detected after 6pm", "Alert me loudly if a car enters
  between 10pm and 6am on all cameras". Overnight windows
  (`22:00`–`06:00`) wrap correctly.
* **One-click presets** on the Automations card. Presets are
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
* **The + form** on the Automations card (+ → 🔔 Alarm): name, target
  — with a pick-list of what the detector can actually see, fed by
  `GET /alarm-targets` — an **explicit camera picker** (all cameras or
  one; nothing arms fleet-wide by accident), alert level, time window.
  The same form, minus the camera picker, sits on each camera's own
  screen, fixed to that camera.

**Alert levels** (per alarm, preselected by target):

| Level | Behaviour |
|---|---|
| **siren** (critical) | latches — rings until a human silences it |
| **pulse** (urgent) | loud for a minute, then stands down on its own |
| **chime** | a single ding, no latch |
| **silent** | notifications only |

Fire-grade targets (fire, smoke, flame, gas) default to **siren**;
everything else to **chime**. The ⚙ button on the Automations card edits
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
in the **Automations card** (+ → 👁 Watch: notify or live count, target
pick-list, explicit camera picker); stop one by voice ("stop watching
the driveway") or with its ✕.

## Background tasks

A **task** is a one-shot, longer-running job that would block the
conversation if run inline: "check every camera for anyone in a red
shirt" fans out over every camera, runs detection and description on
each, and reports back when done — while you keep talking.

* **How it works**: the agent queues the job and answers immediately;
  a background worker runs a full tool-calling turn for the query and
  stores the result. The Automations card polls progress, and the
  completion is surfaced back into the chat. Guardrail: a background
  task runs with the agent-control tools removed — a task can't spawn
  more tasks, arm alarms, or create watches.
* **How to add one**: the Automations card's + → ⚙ Task form always
  works (it posts to `/tasks` directly; an optional camera picker
  scopes the search by appending "on cam1" to the query — the same way
  the voice path scopes it). Creating tasks *by voice* is off in both
  docker demos (the tool is token-hungry on CPU): add
  `create_background_task` to `enabled_tools` and restart, or run a
  bare-metal config with `enabled_tools` unset.
* **Task vs watch**: a task runs once and finishes; a watch runs
  forever until stopped.

## Events

The agent remembers what the detection stream saw, at two depths — and
all of the events tools are on by default in the docker configs.

* **Minutes back**: "did anyone come to the door in the last 30
  minutes?" answers from the NATS event ring (`recent_events`) —
  short-term, in-process memory (`event_ring_size`).
* **Days back**: "did you see a person today?", "which cars entered
  between 1 and 2pm?" answer from the platform's durable **events
  store** (`search_history`) — one row per visit, with the best photo
  kept. Follow up on a specific visit with `describe_event` ("what was
  the person doing in event #12?") or review a whole span with
  `describe_window`. Absolute clock windows ("from 2pm to 3pm") are
  parsed and honoured.

The **History card** in the rail is the browsable face of the store:
filter by label / camera / window, see each visit's kept photo, and tap
💬 to ask the agent about it. **Footage** search (`search_footage`)
stays the tool for attribute questions ("did a *red* truck come by?") —
it reads the footage-search app's caption index.

## Events, alerts, and alarms — the attention ladder

Three words that sound alike but mean three different demands on your
attention. From quietest to loudest:

* An **event** is a recorded fact: *"person seen on cam2 at 14:02."*
  The detection stream produces them continuously. Nobody is notified —
  events are the raw material the agent (and every app) reasons over.
  You query them ("did anyone come to the door in the last 30
  minutes?"); they never come to you.
* An **alert** is an event that matched something someone *said they
  care about* — a watch's condition, an app's rule (the occupancy
  counter crossing its limit), a report arriving. It's delivered as a
  notification: informational, read it when you like, nothing latches.
* A **critical alert** is an alert that demands a human *now* — and
  **alarms are how you ask for one**. An alarm's siren latches and
  rings until acknowledged; that's the whole difference. (An alarm
  created at the *chime* or *silent* level is really asking for an
  ordinary alert on an alarm's engine — the level, not the word, sets
  the urgency. **siren** and **pulse** are the critical grades.)

So: events happen, alerts inform, alarms interrupt. A fact climbs the
ladder only because a rule you created (watch, app, alarm) said it
should.

## Where alerts reach you

Three streams end up in front of you:

* **The notification feed** the demo polls: alarm rings, watch hits,
  task completions — and **app alerts**. The agent subscribes to
  `opennvr.alerts.app.>` on the bus, so any catalog app's alert (on a
  stock install: the default-on occupancy counter's *over-occupancy*)
  lands in the feed proactively, throttled per app+camera so a
  misbehaving app can't spam. Ask "any occupancy alerts this morning?"
  (`recent_app_alerts`) for the full, unthrottled history.
* **The Activity card**: the same items, in the right rail.
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

The scheduled task shape: "every morning at 7, summarise overnight
activity", or "every day at 8 AM, tell me how many trucks entered" — a
standing query the agent re-runs on its clock, with each answer landing
in the Automations card and the notification feed (and any configured
external channel). Create by voice/chat, stop the same way ("stop the
morning report"). Off in both docker demos; enable by adding
`create_report` / `stop_report` to `enabled_tools`.

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
