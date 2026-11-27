# Workflows

A workflow is a short automation a site writes for itself: a **trigger**,
a few **steps**, and what happens on each outcome. One kind of step, the
**AI step**, looks at the picture or the event and decides; plain rules
and people decide everything that moves something. Everything runs on the
box. The design and its reasons are in the RFC,
[Agentic Workflows (#620)](https://github.com/open-nvr/open-nvr/discussions/620).

> **Status.** The engine and its API are on `main`. The Workflows page in
> the web UI is being built ([#773](https://github.com/open-nvr/open-nvr/issues/773));
> until it lands, workflows are written as YAML and managed through the
> API, as below.

- [1. Set up](#1-set-up)
- [2. Your first workflow](#2-your-first-workflow)
- [3. The workflow file](#3-the-workflow-file)
- [4. The safety rule](#4-the-safety-rule)
- [5. Test, On, and asking a person](#5-test-on-and-asking-a-person)
- [6. Reading a run](#6-reading-a-run)
- [7. What is not there yet](#7-what-is-not-there-yet)
- [8. API reference](#8-api-reference)

## 1. Set up

**Permissions.** `workflows.view` (see workflows and runs),
`workflows.manage` (create, edit, switch, try, press a button) and
`workflows.approve` (answer Ask a person). On a fresh install the
operator role has all three and the viewer role has `workflows.view`;
an upgrade gives them to the roles that had `cameras.view` /
`cameras.manage`. Turning **On** a workflow with an action that moves
things (opens a gate, changes the site mode) needs an admin.

**A model, for AI steps.** AI steps call an Ollama (or OpenAI-compatible)
server that core can reach. In `.env`:

```bash
# With the camera-agent overlay's bundled Ollama:
WORKFLOWS_LLM_URL=http://ollama:11434
# Or an Ollama on the host (as for OLLAMA_EXTERNAL_URL):
# WORKFLOWS_LLM_URL=http://host.docker.internal:11434
WORKFLOWS_LLM_MODEL=qwen3-vl:2b-instruct
```

then pull the model (`ollama pull qwen3-vl:2b-instruct`) and recreate
core. A Look step sees pictures, so the model must be a vision model.
With `WORKFLOWS_LLM_URL` empty, every AI step answers `unsure` and
workflows built from rules alone still run.

How long a step takes depends on the hardware. On a 4-core CPU a Look
step takes tens of seconds, which is why pictures are shrunk to 768 px
first (`WORKFLOWS_LOOK_MAX_SIDE`); on a GPU, a few seconds. Measured
numbers are in
[MODELS_AND_LATENCY.md](../examples/camera-agent/MODELS_AND_LATENCY.md#the-look-eval-toolslook_evalpy).

**Try a Look step before any workflow.** `opennvr-app ai try` runs one
Look step on your own snapshots with your model and shows what it
decides, why, and how long it took
([the SDK README](../sdk/opennvr-app-sdk/README.md#try-a-workflows-ai-step)).

## 2. Your first workflow

Log in for a token (an API token cannot reach these routes; add your
authenticator code as `code` if you have one):

```bash
NVR=https://your-nvr
TOK=$(curl -s -X POST $NVR/api/v1/auth/login-json -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "…", "code": "123456"}' | jq -r .access_token)
H="Authorization: Bearer $TOK"
```

Start from a template. Three ship: **Gate concierge** (a Look step says
who is at the gate, then the guard is asked, and the gate opens only on
"Let in"), **Safety gear check** and **Night intrusion triage**. Each lists
the apps and cameras it needs:

```bash
curl -s $NVR/api/v1/workflows/templates -H "$H" | jq '.[] | {id, name, needs}'
curl -s $NVR/api/v1/workflows/templates -H "$H" \
  | jq -r '.[] | select(.id=="gate_concierge") | .yaml' > gate.yml
```

Edit `gate.yml` (your camera names, below), then save it. A new workflow
always starts in **Test**:

```bash
curl -s -X POST $NVR/api/v1/workflows -H "$H" -H 'Content-Type: application/json' \
  -d "$(jq -Rs '{yaml: .}' gate.yml)" | jq '{id, state, needs, warnings}'
```

A file that cannot run is refused with `422` and every problem at its key,
for example `{"path": "then[0].ai.rules", "message": "..."}`.

See what it would have decided on the last week's alerts:

```bash
TRY=$(curl -s -X POST $NVR/api/v1/workflows/1/try -H "$H" \
  -H 'Content-Type: application/json' -d '{"limit": 5}' | jq -r .try_id)
curl -s $NVR/api/v1/workflows/tries/$TRY -H "$H" | jq '.state, .done, .total'
```

Then let it run in Test for a day, read its runs (section 6), and turn it
On:

```bash
curl -s -X POST $NVR/api/v1/workflows/1/state -H "$H" \
  -H 'Content-Type: application/json' -d '{"state": "on"}'
```

## 3. The workflow file

```yaml
workflow: v1                      # the contract version, required
name: Night intrusion triage
limits: { cooldown: 2m, one_at_a_time: track, per_hour: 20, ai_per_hour: 40 }
when:                             # exactly one trigger
  alert: { app: intrusion-detection, type: intrusion, cameras: [yard] }
if:                               # guards: all must hold, or no run starts
  - site_mode: [armed_home, armed_away]
  - time: { from: "22:00", to: "06:00" }
then:                             # the steps, top to bottom
  - ai: { ... }                   # each outcome's path inside it
always: [ log: "{reason}" ]       # runs after whichever path ran
```

The full grammar, with every check, is the SDK's validator,
[`opennvr_app_sdk.workflows.validate`](../sdk/opennvr-app-sdk/opennvr_app_sdk/workflows/validate.py):
the engine, the coming editor and any outside tool run the same check.

**Cameras are referenced by role**, never by id, so a workflow moves
between sites. Until cameras carry a role of their own, a camera's role is
its name, lower-cased with spaces as `-`: a camera named "Front Gate" is
`front-gate`.

### Triggers (`when`)

| Trigger | Starts a run when | Values it gives the steps |
|---|---|---|
| `alert: {app, type?, cameras?}` | an app raises that alert | `camera`, `camera_id`, `camera_name`, `app`, `type`, `title`, `severity`, and the alert's evidence values (`plate`, `track_id`…) |
| `event: {subject, cameras?}` | a contracted event, e.g. `plate.recognized.v1` ([EVENT_CONTRACTS.md](EVENT_CONTRACTS.md)) | `camera`, `camera_id`, `schema`, `ts`, and the payload's values (`plate_text`…) |
| `camera_offline: {cameras, for?}` | a camera has been down for `for` (default `2m`), once per outage | `camera`, `camera_id`, `offline_for` (seconds) |
| `schedule: {at, days?}` | `at` "HH:MM" local time, on `days` (`mon`…`sun`) if given | `time`, `day` |
| `button: {label?}` | someone presses it: `POST /workflows/{id}/run` | `pressed_by` |

A workflow never starts on an alert raised by a workflow.

### Conditions (`if:` guards, and the `if` step)

`site_mode: [modes]`, `time: {from, to}` (may wrap midnight),
`camera: [roles]`, `value: {of: <name>, in: [values]}`.
`list: {of, in}` (a site list) is accepted but holds false until Site
rules exist.

### Steps

| Step | What it does | Its paths |
|---|---|---|
| `ai:` | Look (a picture, frames or text) or Investigate, decides one of `decision` | one per outcome, plus `unsure` |
| `if:` | a plain rule | `then`, `else` |
| `for_each: {cameras}` | repeats `do` for each camera role | `do` |
| `ask_person: {who, message, replies, timeout?, default?}` | asks a person and waits | one per reply, plus `no_answer` |
| an action | below | none |

A step with paths runs the one chosen, then the flow carries on with the
next step. Paths never loop back.

**The AI step.** Its options are `decision`; `unsure` is always added and
always needs a path. With `rules`, the model reports the `fields` (facts)
and the first matching rule gives the outcome, so the outcome can never
contradict the facts; a Look step may do without rules, an Investigate
step must have them. `sure` (default 0.75) is the confidence floor,
applied to agreement across repeated draws on a GPU. A model that is
down, out of time or answering outside the options gives `unsure`, never
an error. The step is the same one `opennvr-app ai try` runs; its keys are
in the SDK
([`ai_step.py`](../sdk/opennvr-app-sdk/opennvr_app_sdk/workflows/ai_step.py)).
Event values reach the model as data only, never as instructions, so
`{plate}` is not allowed inside a prompt.

### Actions

| Action | What it does |
|---|---|
| `log: "text"` | a line on the run |
| `alert: {severity, title}` | an inbox alert from the workflow; it rings and reaches alarm actions like any alert |
| `notify: {who, message, severity?}` | an inbox entry for `who` (default `low`) |
| `save_clip: {camera, before?, after?}` | protects the camera's recordings around the moment the run began from retention (30 s each side by default) |
| `ptz_preset: {camera, preset}` | points a PTZ camera at a preset, by name |
| `webhook: {to, body?}` | posts to an enabled webhook, Slack, Teams or Telegram integration named `to` (Settings > Integrations: the admin-approved destinations); Telegram gets the message as text, with the body's values as lines |
| `app: {id, action, args?}` | an installed app's action, e.g. the gate controller's `open_now` |
| `home_assistant: {service, data?, to?}` | calls a Home Assistant service, e.g. `service: lock.unlock`, `data: { entity_id: lock.gate }`, through an enabled **Home Assistant** integration (Settings > Integrations: its URL and a long-lived token), the one named `to` or the site's only one |
| `site_mode: mode` | sets the site mode |
| `wait: 30s` | pauses the run |

**Placeholders.** `{name}` in an action's text is filled from the
trigger's values (table above), then each AI step's (`{outcome}`,
`{reason}`, `{confidence}` and its fields), then the last answer
(`{reply}`). A camera can be `{camera}`, the one that triggered the run.

### Limits

| Limit | Default | |
|---|---|---|
| `cooldown` | `1m` | between runs for the same camera, track or plate |
| `one_at_a_time` | `camera` | `camera`, `track`, `plate` or `none`: no new run while one for the same key is going |
| `per_hour` | 30 | runs an hour |
| `ai_per_hour` | 60 | AI steps an hour; past it, AI steps answer `unsure` |

## 4. The safety rule

*AI informs and recommends; people and plain rules move things*
(RFC #620, section 7). The validator refuses a file that breaks it:

- An action that **moves things** (`site_mode`, `home_assistant`, and any
  app action the app declares `confirm=True`, or an unknown one) may only
  follow an Ask a person **reply** or a plain rule (`if`, or the file's
  `if:` guards) on the same path, never straight under an AI outcome, and
  never under `no_answer`: nobody chose that.
- A `critical` alert follows the same rule. An AI-decided `high` alert is
  allowed but capped at `medium` when it runs.
- An AI step's tools only read or ask a person; a tool that moves things
  cannot be listed, and the SDK refuses one even if a validator missed it.

Only an admin turns On a workflow that moves things. A new version whose
AI steps changed goes back to Test, and so does one that gains a moving
action while On under a non-admin.

## 5. Test, On, and asking a person

| State | |
|---|---|
| `off` | does nothing |
| `test` | runs for real up to the actions: AI steps decide, but every action is recorded as **"would have"**, and Ask a person asks nobody and takes its `default` |
| `on` | does it |

**Ask a person** creates an approval and the run waits. A person with
`workflows.approve` who can see the run's camera answers it:

```bash
curl -s "$NVR/api/v1/approvals?open=true" -H "$H" | jq '.[] | {id, workflow_name, question, replies}'
curl -s -X POST $NVR/api/v1/approvals/7 -H "$H" -H 'Content-Type: application/json' \
  -d '{"reply": "Let in"}'
```

The run carries on down that reply's path, after a server restart too.
When `timeout` (default `5m`) passes with no answer, the run takes the
`no_answer` path; the `default` reply is used only in Test, so a default
of "Let in" can never open a gate on silence. Approvals are their own
records: acknowledging alerts never clears one.

**Trying on recent events** (`POST /workflows/{id}/try`) replays up to 20
of the last days' alerts the trigger matches, through the steps as in
Test, and returns each one's decisions and AI trace. It can try an
unsaved edit (`{"yaml": ...}`). A trial is not a run: it never shows among
the runs or counts against the limits. The file's `if:` guards are not
checked, since they describe now, not when the alert fired. Alert
triggers only, for now.

## 6. Reading a run

```bash
curl -s "$NVR/api/v1/workflows/1/runs?limit=10" -H "$H" | jq '.[] | {id, status, outcome, test}'
curl -s $NVR/api/v1/workflows/runs/42 -H "$H" | jq '.steps[] | {step_path, kind, outcome, reason}'
```

Each step is recorded as it happens, with its place in the file
(`then[0].ai.delivery[0]`), its outcome and timing. An AI step adds its
reason, confidence, the rule that fired, the facts it reported, and every
model and tool call with what was asked and what came back: a run
explains itself. A run is `running`, `waiting` (on a person), `done` or
`failed`; a run a restart interrupted is marked failed, never redone, as
an action may already have happened.

## 7. What is not there yet

- **The Workflows page** in the web UI ([#773](https://github.com/open-nvr/open-nvr/issues/773)).
- **Investigate has no tools in core yet**: it decides from the event
  alone. It is meant to go live only on hardware where a model passes the
  Investigate cases; no CPU model does today
  ([MODELS_AND_LATENCY.md](../examples/camera-agent/MODELS_AND_LATENCY.md)).
- **Site rules** and site lists (`list:` conditions), **marks** on runs and
  the severity cap tied to them, **Call** (Phase 3).
- **Ask a person inside For each** is recorded as not supported.
- **Camera roles** are camera names until cameras have roles of their own.

## 8. API reference

All under `/api/v1`, with a login token.

| | | Permission |
|---|---|---|
| `GET workflows` | every workflow, with its current file and last run | `workflows.view` |
| `GET workflows/templates` | the templates, with what each needs | `workflows.view` |
| `POST workflows` `{yaml}` | save a new workflow (in Test); `422 {errors, warnings}` if it cannot run | `workflows.manage` |
| `PUT workflows/{id}` `{yaml}` | save a new version | `workflows.manage` |
| `POST workflows/{id}/state` `{state}` | `off`, `test` or `on` | `workflows.manage` (admin to turn On one that moves things) |
| `POST workflows/{id}/run` | press its button | `workflows.manage` |
| `POST workflows/{id}/try` `{yaml?, limit?, days?}` | try on recent events; returns `try_id` | `workflows.manage` |
| `GET workflows/tries/{try_id}` | a trial's progress and results | `workflows.view` |
| `GET workflows/{id}/runs?limit&before` | its runs, newest first | `workflows.view` |
| `GET workflows/runs/{run_id}` | one run, step by step | `workflows.view` |
| `GET approvals?open=true` | approvals waiting (for cameras you can see) | `workflows.approve` |
| `POST approvals/{id}` `{reply}` | answer one | `workflows.approve` |
