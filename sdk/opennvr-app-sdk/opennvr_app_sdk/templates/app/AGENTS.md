<!-- GENERATED from docs/BUILDING_EFFICIENT_APPS.md in the OpenNVR repository
     by scripts/gen_app_agent_guide.py. The rules are edited there, not here.
     Notes about THIS app belong below the last section. -->

# Working on __APP_NAME__

__APP_NAME__ is an app for OpenNVR, a video recorder that runs detection
and the costly models once and publishes the results. An app subscribes
to those results. It does not decode video, run a detector or call a
model for something the platform has already computed. The ten rules
below are how; the reasons, the measurements and a reference app for
each are in the guide:
__DOCS__BUILDING_EFFICIENT_APPS.md

## Commands

```bash
uv sync                               # the SDK and pytest
uv run pytest -q                      # the app's tests; keep them green
uv run opennvr-app dev                # run the rule against a simulated camera, no stack needed
uv run opennvr-app validate --strict  # manifest, config, listing and the rules below
```

Run `validate --strict` before saying a change is done. A finding names
the file, the line and the rule. Fix it. A real exception is waived on
the flagged line or the one above, and the reason is required:

```python
cap = cv2.VideoCapture(path)   # opennvr: allow[own-decode] reads a file the operator uploaded, not a camera
```

Do not add a waiver to make a finding go away. Ask the person you are
working for first; a waiver is their decision and a reviewer reads it.

## The rules

### 1. Take detections from the bus

Subscribe to the detections the platform already publishes. Never open a camera stream, decode video or run an object detector in an app.

A `Detector` (or the `App` facade, which compiles to one) receives tracks as ordinary detections with `track_id`, `stationary` and `best` already worked out. Subscribe to the Tier-0 subject only:

```yaml
# config.yml
subject_pattern: "opennvr.inference.tier0.>"
consume_tier0: true
```

Checked by: `own-decode` (a `VideoCapture`, an `rtsp://` address, `ffmpeg`), `wide-subject`.

### 2. Call a costly model when something happened

Call a model on a trigger (a track, a finished visit, a domain event, an operator's action), on the detector's best frame or a stored evidence photo. Never on every frame, and never on a fixed timer with nothing in the scene.

Let Tier-0 say when to look. A track with `best: true` has a sharp crop waiting; look at that, once per track, and again only on a reason:

```python
def on_detections(self, camera_id, detections, event):
    for det in detections:
        if det.get("label") != "person" or not det.get("best"):
            continue
        track = det.get("track_id")
        if track and self._due(camera_id, track):      # once per track, then on a recheck interval
            self._queue_check(camera_id, track)         # one call at a time, off the event loop
    return []
```

Checked by: `per-frame-model` (a model call inside `FrameApp.on_frame`, an `InferStream` session).

### 3. Every model call goes through KAI-C

Reach a model only with `nvr.ai`. Never hold an adapter's address, never post to an adapter's `/infer`, and never load a model runtime inside the app.

```python
caps = nvr.ai.capabilities()            # which adapters, which tasks; None = could not ask
body = nvr.ai.infer(adapter, jpeg, task="vqa", camera_id=cam.handle,
                    params={"question": "How many parcels are on the doorstep?"})
```

Checked by: `direct-adapter`, `own-model`.

### 4. Ask what the platform already knows

Before asking a model about a visit, read what the platform has already computed for it: the caption and attributes, the plate, the embedding-backed search, the stored photo.

| You want | Read this, do not compute it |
|---|---|
| A sentence or attributes for a visit, pushed as they land | subscribe to `visit.enriched.v1` (`VisitEnriched`; published since 0.1.11) |
| "Find the red van" | `nvr.timeline.find("red van")` — no index of your own |
| Visits by label and time | `nvr.timeline.search(camera=, label=, start=)` |
| A plate | subscribe to `plate.recognized.v1`; history from `nvr.timeline.plate_*` |
| Who is inside | `nvr.timeline.plates_inside()` |
| The photo of a visit | `nvr.timeline.evidence(event_id)` |
| The picture to show beside a plate | `nvr.timeline.plate_frame(event_id)` — the frame the plate was read from; `evidence()` is the best view of the track and can be another car |
| Is anyone there now, how many | the latest Tier-0 tracks; no model call |
| What is on the camera now, in words | `nvr.description(camera, max_age_s=30)` — one shared look for every asker |

### 5. Build on domain events

Put business logic on domain events (`opennvr.events.<domain>.<event>.v1.<camera>`). Publish what the app learns as a domain event. Adapter completion subjects are plumbing.

`DomainEventSubscriber` with `subscriptions = [...]` and `on_event(event)`; `event.typed()` gives the payload as its contract class. Publish with `DomainEventPublisher.publish_typed(...)`. A new event is added to EVENT_CONTRACTS.md first.

### 6. Work only on the cameras selected for the app

Act on the cameras an operator selected for the app and on no others. An empty selection means do nothing.

`Detector` drops events from unselected cameras before `on_detections`, and `FrameApp` polls only the selected ones; do not work around either. An app that manages its own workers reads `nvr.roster()`: `[]` means stop everything, `None` means core could not be asked, so keep what is running. Override `on_cameras_update` to react when the selection changes. An app that reads no camera data declares `camera_picker=False`.

### 7. "Could not ask" is not "no"

A read that returns `None` means the platform could not be asked. Never report it as an empty result, and never probe the platform per frame or per event.

Keep the last good answer for as long as it is plausible, and say when it is stale. Cache `ai.capabilities()` and the roster for minutes, not seconds, and refresh on `on_cameras_update` and on a timer.

### 8. Bound every call, back off, and yield

Every call has a timeout. A failure is followed by a growing wait, not an immediate retry. Background work stops while a person is waiting for an answer.

One model call in flight per app unless there is a reason for more. Back off on failure (the guard-scan app waits two seconds times the failure count, up to thirty). Show the degraded state on `/health` instead of failing silently. Drop work that is no longer useful: a check for a track that has left, a second request for the answer already on its way.

### 9. Keep state, photos and findings in the platform

Durable state goes in `nvr.state`, photos in the evidence store, and what the app learns about a visit on the visit. No private database, no image bytes on the bus, no parallel visit log.

```python
nvr.state.set("alerted", sorted(alerted))                 # survives a restart
path = nvr.save_evidence(jpeg)                             # content-addressed path
alert = Alert(..., images={"snapshot": path})              # a path, never bytes
visit = nvr.timeline.visit_at(camera, at, label="person")  # which visit was this?
nvr.timeline.add_claims(visit["event_id"], claims)         # attach what was learned
```

### 10. Declare what the app needs

Say in the manifest which tasks the app needs, which extra classes Tier-0 should track for it, which hosts it reaches outside the site, and whether it reads camera data.

`requires_tasks` with canonical task names (`server/config/tasks.yml`), `tier0_labels` in lowercase, `network_egress` in the listing (`[]` when the app never leaves the site, APP_NETWORK.md), `camera_picker=False` when no camera data is read.

Checked by: `opennvr-app validate` (manifest and listing checks).

## Before you finish

Before an app is listed, each line is true or has a written reason:

- [ ] It opens no camera stream and decodes no video. Detections come from `opennvr.inference.tier0.>` with `consume_tier0` on the config object.
- [ ] A model is called only on a trigger, on the best frame or an evidence photo, one call at a time.
- [ ] Every model call is `nvr.ai.infer(...)` with `camera_id`. No adapter address is in the code or the config. No model runtime is imported.
- [ ] It reads `visit.enriched.v1`, `plate.recognized.v1`, `timeline.find` and Tier-0 tracks before asking a model for the same thing.
- [ ] Business logic subscribes to domain events, and what it learns is published as one.
- [ ] It acts only on selected cameras; an empty selection stops it; an unreachable core does not.
- [ ] `None` is never shown as "none". Probes are cached for minutes.
- [ ] Every call has a timeout and a back-off, and a degraded state is visible on `/health`.
- [ ] Cooldowns that must survive a restart are in `nvr.state`; alerts carry evidence paths; findings about a visit are written with `add_claims`.
- [ ] The manifest declares `requires_tasks`, `tier0_labels`, and the listing declares `network_egress`.
- [ ] `opennvr-app validate --strict` passes, or each finding has a waiver with its reason.

## Where to look

- The app's rule and manifest: `__APP_MODULE__.py`. Its knobs: `config.example.yml`.
- The platform client (`OpenNVR`): __DOCS__APP_PLATFORM.md
- Every exported SDK name: __DOCS__SDK_REFERENCE.md
- The Tier-0 event shape and its traps: __DOCS__tier0-consumption.md
- Every event an app may subscribe to or publish: __DOCS__EVENT_CONTRACTS.md
- One runnable file per SDK class: the `cookbook/` directory of the
  `opennvr-app-sdk` source.

When the SDK has no call for something the app needs from the platform,
say so and stop. Do not reach core, KAI-C, an adapter or a camera with a
hand-written client: that is the thing these rules exist to prevent.
