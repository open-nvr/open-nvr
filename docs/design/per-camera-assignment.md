# Per-camera assignment & separation of concerns

**Goal.** "Camera 1 does LPR. Cameras 2 and 3 do people counting. Camera 4
does object detection plus trucks." Declared once, honoured everywhere,
with each layer owning exactly one job.

This document states the layering OpenNVR already has, names the one place
it breaks today, and specifies the fix.

## The layering (already true)

The publish/subscribe separation the platform is built on:

```
 camera ──► MediaMTX ──┬──► Tier-0 detect-pipeline ──┐
                       │                             │   opennvr.inference.
                       └──► adapter (via KAI-C) ─────┤   <adapter>.<camera>.completed
                                                     ▼
                                            ┌────────────────┐
                                            │  NATS  (bus)   │
                                            └────────┬───────┘
                       opennvr.alerts.               │  App SDK: Detector
                       <kind>.<name>.<camera>        │  (subscribe)
                                            ┌────────▼───────┐
                       ┌────────────────────┤      apps      │
                       │  AlertSubscriber   │  (rules only)  │
                       ▼  (subscribe)       └────────┬───────┘
                 other apps / agent / UI             │  AlertDispatcher
                                                     └──► back onto the bus
```

Four properties hold today, and every future change must preserve them:

1. **Models never know about rules.** An adapter or Tier-0 publishes what it
   saw. It has no idea a loitering rule exists.
2. **Apps never run inference to answer "what is there".** They subscribe.
   One inference stream feeds N apps — the reason two apps cost the same
   CPU as one.
3. **Both buses are camera-addressable.** `opennvr.inference.<adapter>.<camera>.completed`
   and `opennvr.alerts.<kind>.<name>.<camera>`. An app that should only
   watch camera 1 subscribes to `opennvr.inference.*.cam1.completed` — the
   filtering is in the subject, not in application code.
4. **Apps compose.** An app publishes alerts through the same fan-out other
   apps subscribe to, so a rule can be built on another rule's output
   without either knowing the other exists.

The App SDK is the only surface an app needs for this: `Detector` to
subscribe, `AlertDispatcher`/`NatsAlertChannel` to publish, `FrameSource`
and `KaiCClient` when a frame or an on-demand expensive model is genuinely
required, and `tier0.py` to reuse Tier-0's best frame instead of grabbing
an arbitrary live one.

## Where it breaks: assignment lives in three places

There is no single answer to "what is camera 4 for". The intent is spread
across three systems that don't know about each other:

| Where | What it holds | Format |
|---|---|---|
| `AIModel.assigned_camera_id` + `task` | a model bound to one camera, driven by a per-model polling loop | DB rows, AI Models page |
| detect-pipeline env | `DETECT_LABELS`, `DETECT_FPS`, gate mode | **global** — every camera gets identical settings |
| each app's `config.yml` | its own `cameras:` list, zones, thresholds | per-app YAML |

Three consequences, all real today:

* **Tier-0 cannot be told "camera 4 also wants trucks".** `DETECT_LABELS` is
  process-wide, so per-camera class selection is impossible — the exact
  case in the goal above.
* **The same intent is retyped per app**, in ids that must match exactly.
  `cam-1` vs `cam1` silently counts nothing (fixed for occupancy by asking
  OpenNVR, but that watches *all* cameras — the opposite of assignment).
* **`AIModel`'s loop is a second inference path** that bypasses the
  publish/subscribe design: it drives per-camera inference on a timer,
  which is how a CPU-only box ends up with a VLM pinned at 600%.

## The fix: one assignment, three consumers

**One source of truth.** A camera↔capability assignment stored in core:

```jsonc
// GET /api/v1/internal/camera-agent/cameras   (extended, back-compatible)
{ "cameras": [
  { "camera_id": "cam1", "name": "Gate",
    "assignments": [ { "skill": "license_plate_recognition" } ] },
  { "camera_id": "cam4", "name": "Yard",
    "assignments": [ { "skill": "object_detection",
                       "labels": ["person", "car", "truck"] } ] }
] }
```

`assignments` is additive: every existing consumer ignores it until it
opts in. One UI surface (the camera's settings page) writes it; nothing
else does.

**Consumer 1 — Tier-0** reads its per-camera slice on the reconcile tick it
already runs (`/detect-config` is the existing precedent for live config
without redeploy). `labels` becomes per-camera, defaulting to the global
`DETECT_LABELS` when unset. Cameras assigned nothing detection-shaped can
be skipped entirely (`analyze: false`), which is a CPU saving, not a cost.

**Consumer 2 — apps** stop carrying camera lists. The SDK gains:

```python
from opennvr_app_sdk.cameras import cameras_for_skill

cams = cameras_for_skill(opennvr_url, skill="occupancy_counting")
# -> ["cam2", "cam3"]   — and the app subscribes to exactly those subjects
```

An app then declares *what it is* (already in its `AppManifest`) and asks
which cameras were assigned to it. `cameras:` in app YAML remains as an
explicit override, never as the thing an operator must fill in.

**Consumer 3 — the UI** renders assignment per camera and, because the
manifest declares `requires_tasks`, can refuse an assignment whose
capability isn't installed ("people counting needs object detection —
Tier-0 provides it ✓" / "LPR needs the plate adapter — not installed").

## Rules that keep the concerns separate

These are the invariants a future PR should be checked against:

1. **An app must not open a camera stream to ask what is in frame.** If it
   needs pixels, it takes Tier-0's best frame for the track; if it needs an
   expensive model, it dispatches through KAI-C so the call is governed,
   audited and rate-limited. `FrameApp` exists for genuine exceptions (a
   doorbell that must run recognition the instant a face appears), not as
   the default archetype.
2. **A model must not encode a rule.** No thresholds, zones, or dwell times
   inside an adapter. "Person present" is a model output; "person present
   for 30s in this polygon" is an app.
3. **Assignment is not configuration.** Which camera does what is operator
   intent and lives in core. Model tuning (fps, confidence, labels) is
   deployment configuration and lives with the component. The assignment
   may *narrow* configuration (per-camera labels), never replace it.
4. **Every stream is subject-addressable.** New event types get
   `<domain>.<producer>.<camera>.<verb>` subjects so a subscriber can
   filter in the broker instead of in a loop.
5. **One inference per frame per model.** If two apps want the same thing,
   they subscribe to the same subject. Adding an app must never add
   inference cost.

## Migration path

Each slice is independently shippable and useful on its own:

1. **Schema + endpoint.** `assignments` on the camera record, written by the
   camera settings page, served by the internal endpoint. Nothing consumes
   it yet. *(shipped — PR #275)*
2. **SDK `cameras_for_skill()`.** Apps opt in; occupancy switches from
   "watch every camera" to "watch my assigned cameras", explicit config
   still winning. *(shipped — `filter_cameras_for_skill` /
   `cameras_for_skill` in `opennvr_app_sdk.cameras`; occupancy is the
   reference consumer, re-scoping on every discovery refresh)*
3. **Tier-0 per-camera labels/analyze.** The class-selection case from the
   goal, plus the CPU saving of not analysing unassigned cameras.
   *(shipped — provider parses ``assignments`` into per-camera labels;
   label changes restart just that worker on the reconcile tick; the
   skip is opt-in via ``DETECT_SKIP_UNASSIGNED``, off by default because
   the Tier-0 stream feeds non-detection consumers too)*
4. **Retire `AIModel`'s polling loop** in favour of assignment + the
   publish/subscribe path, removing the second inference route entirely.
   *(shipped — the live and cloud polling loops are removed from
   InferenceManager; POST /start-inference answers 410 Gone with the
   migration pointer; the AI Models and camera-settings UIs point at
   Assignments instead of a Start button. The on-demand RECORDING
   analysis remains — a forensic pass over recorded files was never a
   second live path.)*

Slices 1–2 make the goal expressible; slice 3 makes it efficient; slice 4
removes the architectural exception that undermines the whole model.
