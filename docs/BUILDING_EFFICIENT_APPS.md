# Building efficient apps

An OpenNVR box does the expensive work once. It decodes each camera
once, runs one detector on it, keeps one best photo of each visit, and
runs the costly models (a caption model, an embedder, a plate reader)
on that photo once. Everything an app needs is a result of that work,
published on the bus or kept in the store.

An app that does any of it again pays for it a second time, on the
same processor, outside the budget that protects recording and
detection. The next app pays a third time. This page is the set of
rules that keeps that from happening, each with its reason, a number
where one was measured, and an app in this repository that does it the
right way.

It is the one place the rules live. The facts behind them are in the
reference pages ([APP_PLATFORM.md](APP_PLATFORM.md),
[tier0-consumption.md](tier0-consumption.md),
[EVENT_CONTRACTS.md](EVENT_CONTRACTS.md), [ENRICHMENT.md](ENRICHMENT.md),
[KAI_C_USAGE.md](KAI_C_USAGE.md),
[RFC-0004](rfcs/RFC-0004-infer-once-publish-once.md)); this page says
what to do with them.

**For coding assistants.** Every app scaffolded by `opennvr-app new`
carries these rules as `AGENTS.md` and as a Claude Code skill
(`.claude/skills/opennvr-app/SKILL.md`). Both files are generated from
this page by `scripts/gen_app_agent_guide.py`, and a test fails when
they drift from it. Edit the rules here, then run the script.

**Checked by a program.** `opennvr-app validate` reads an app's source
for the patterns below that a program can see, and names the rule.
`--strict` fails on them. See [The checks](#the-checks).

## The picture

```
camera ──► decode once ──► Tier-0 detector ──► tracks, visits, best frame
                                   │
                                   ├─► bus: detection.observed.v1, visit.recorded.v1
                                   │
                        enrichment tier (through KAI-C, on the best frame, once)
                                   │
                                   ├─► store: caption, attributes, embedding, plate
                                   └─► bus: visit.enriched.v1, plate.recognized.v1

your app ──► subscribes to the bus, reads the store, asks KAI-C only for
             what nobody has computed yet, and only when something happened
```

Two words used below. **Tier-0** is the detector every camera runs all
the time (the `detect-pipeline` container). **KAI-C** is the
orchestrator in front of every model adapter; it is the only door to a
model.

## The rules at a glance

| # | Rule | Checked by |
|---|---|---|
| 1 | Take detections from the bus; never decode video or run a detector | `own-decode`, `wide-subject` |
| 2 | Call a costly model when something happened, on the best frame; never per frame | `per-frame-model` |
| 3 | Every model call goes through KAI-C; no adapter addresses, no model inside the app | `direct-adapter`, `own-model` |
| 4 | Ask what the platform already knows before asking a model | review |
| 5 | Build on domain events, not on adapter completions | review |
| 6 | Work only on the cameras selected for the app | SDK (built in) |
| 7 | "Could not ask" is not "no"; cache what you probe | review |
| 8 | Bound every call, back off, and yield to the detector | review |
| 9 | Keep state, photos and findings in the platform | review |
| 10 | Declare what the app needs in the manifest | `opennvr-app validate` |

---

## Rule 1 — Take detections from the bus

**Rule.** Subscribe to the detections the platform already publishes.
Never open a camera stream, decode video or run an object detector in
an app.

**Why.** Decoding is the largest cost on the box, larger than the
detector. A 2880×1616 stream at 25 frames a second is about 116 million
pixels a second to decompress, and the cost is the same whether two
frames a second are analysed or twenty-five
([DETECT_CPU.md](DETECT_CPU.md)). The platform pays that once per
camera. An app that opens the stream pays it again, per app.

**How.** A `Detector` (or the `App` facade, which compiles to one)
receives tracks as ordinary detections with `track_id`, `stationary`
and `best` already worked out. Subscribe to the Tier-0 subject only:

```yaml
# config.yml
subject_pattern: "opennvr.inference.tier0.>"
consume_tier0: true
```

`consume_tier0` must be a field of the app's config class as well as a
line in the YAML; the SDK reads it from the object. The wide subject
`opennvr.inference.>` with Tier-0 consumed delivers every object twice
on the day a heavy detector runs on the same camera. Read `stationary`
instead of deriving it, and `track_id` instead of matching boxes
between frames. A class Tier-0 does not track by default (a bag, a
parcel) goes in the manifest as `tier0_labels`, and the platform
widens the detector on the cameras selected for the app
([tier0-consumption.md](tier0-consumption.md)).

**Reference.** [`examples/occupancy-counting`](../examples/occupancy-counting/)
(narrow subject and the flag, in the config class and both config
files). [`examples/abandoned-object`](../examples/abandoned-object/)
declares `tier0_labels`.

**Check.** `own-decode` (a `VideoCapture`, an `rtsp://` address,
`ffmpeg`), `wide-subject`.

## Rule 2 — Call a costly model when something happened

**Rule.** Call a model on a trigger (a track, a finished visit, a
domain event, an operator's action), on the detector's best frame or a
stored evidence photo. Never on every frame, and never on a fixed timer
with nothing in the scene.

**Why.** A model call on an accelerator cannot be interrupted, so the
detector waits for it. On the Ventuno Q's Hexagon NPU one caption or
question is a unit of about 1.6 to 2 seconds
([RFC-0004](rfcs/RFC-0004-infer-once-publish-once.md)). Calls made per
frame or per question, 325 of them in ten minutes, took 48 % of the NPU
and left the detector at four times its quiet latency (same RFC,
measured 2026-11-12). On a CPU a caption takes seconds; one busy camera
outran the captioner, every call timed out while the model finished the
work anyway, and the box lost two cores for no captions
([ENRICHMENT.md](ENRICHMENT.md), issue 583).

**How.** Let Tier-0 say when to look. A track with `best: true` has a
sharp crop waiting; look at that, once per track, and again only on a
reason:

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

The picture comes from the platform: the track's best frame
(`BestFrameClient`), the visit's evidence photo
(`nvr.timeline.evidence(event_id)`), or a current frame
(`nvr.snapshot(camera)`) when the subject is the scene and not a
track. Queue the calls, run one at a time, and drop a queued check
whose track has gone.

A rule that truly needs every frame (pose keypoints at ten frames a
second) is the exception. It takes its frames from `nvr.stream(camera)`
and its model through `nvr.ai.stream()`, says so in the app's
description, and carries a waiver the validator shows to a reviewer.
The cost of that app is one model session per camera for as long as it
runs, and the operator should be told.

**Reference.** [`examples/ppe-check`](../examples/ppe-check/) (`on_detections`:
a model call once per person track with a best frame, a recheck
interval, one check at a time).
[`examples/package-delivery`](../examples/package-delivery/) (a count
of the doorstep only after a person who stood in the zone has left).

**Check.** `per-frame-model` (a model call inside `FrameApp.on_frame`,
an `InferStream` session).

## Rule 3 — Every model call goes through KAI-C

**Rule.** Reach a model only with `nvr.ai`. Never hold an adapter's
address, never post to an adapter's `/infer`, and never load a model
runtime inside the app.

**Why.** KAI-C is where the per-adapter queue, the audit line, the
model-fingerprint check and the site's sovereignty rule are applied
([KAI_C_USAGE.md](KAI_C_USAGE.md)). A call that goes around it has none
of them, and the box's governor cannot see or slow it. A model loaded
inside an app is invisible to every other app, so the second app that
needs the same answer loads a second copy.

**How.**

```python
caps = nvr.ai.capabilities()            # which adapters, which tasks; None = could not ask
body = nvr.ai.infer(adapter, jpeg, task="vqa", camera_id=cam.handle,
                    params={"question": "How many parcels are on the doorstep?"})
```

Pass `camera_id` whenever the frame belongs to a camera; it drives the
queue and the audit. Choose the adapter by the task it advertises, not
by a name in the code. An app that brings its own model ships it as an
adapter ([CONTRIBUTING_ADAPTERS.md](CONTRIBUTING_ADAPTERS.md)) and
names its task in `requires_tasks`.

**Reference.** [`examples/package-delivery`](../examples/package-delivery/)
(`count_parcels`: the adapter is picked from `ai.capabilities()` by
task and called with `nvr.ai.infer`).

**Check.** `direct-adapter`, `own-model`.

## Rule 4 — Ask what the platform already knows

**Rule.** Before asking a model about a visit, read what the platform
has already computed for it: the caption and attributes, the plate, the
embedding-backed search, the stored photo.

**Why.** The enrichment tier describes a visit once and keeps the
answer. Asking again is the same model call for the same pixels. The
size of that waste was measured: on a looping clip, 9,921 of 10,892
caption calls in a day went to a frame identical to one already
captioned ([`bench/rfc-0004-infer-once-log.md`](../bench/rfc-0004-infer-once-log.md));
core now reuses those, and an app that calls the model itself gets no
such reuse.

**How.**

| You want | Read this, do not compute it |
|---|---|
| A sentence or attributes for a visit, pushed as they land | subscribe to `visit.enriched.v1` (`VisitEnriched`; published since 0.1.11) |
| "Find the red van" | `nvr.timeline.find("red van")` — no index of your own |
| Visits by label and time | `nvr.timeline.search(camera=, label=, start=)` |
| A plate | subscribe to `plate.recognized.v1`; history from `nvr.timeline.plate_*` |
| Who is inside | `nvr.timeline.plates_inside()` |
| The photo of a visit | `nvr.timeline.evidence(event_id)` |
| Is anyone there now, how many | the latest Tier-0 tracks; no model call |

A search answer may carry `pending`: visits in the window that are not
described yet. That is "not yet", and the platform has queued them;
show it and ask again, do not describe them yourself.

**Reference.** [`examples/license-plate-recognition`](../examples/license-plate-recognition/)
(no inference at all: it consumes `plate.recognized.v1` and asks core
who is inside). [`examples/footage-search`](../examples/footage-search/)
(search is one `timeline.find` call; its own index was removed).
[`examples/camera-agent`](../examples/camera-agent/) (`camera_snapshot`
answers count and presence from Tier-0 tracks with no inference).

**Check.** Review. See [Planned](#planned-not-yet-rules) for the shared
live description.

## Rule 5 — Build on domain events

**Rule.** Put business logic on domain events
(`opennvr.events.<domain>.<event>.v1.<camera>`). Publish what the app
learns as a domain event. Adapter completion subjects are plumbing.

**Why.** A domain event has a versioned contract and does not say which
model produced it, so the app keeps working when the model behind it
changes ([EVENT_CONTRACTS.md](EVENT_CONTRACTS.md)). It is also how one
app's result reaches the next app without a second inference: the gate
controller acts on the plate app's decision, and neither reads a frame.

**How.** `DomainEventSubscriber` with `subscriptions = [...]` and
`on_event(event)`; `event.typed()` gives the payload as its contract
class. Publish with `DomainEventPublisher.publish_typed(...)`. A new
event is added to EVENT_CONTRACTS.md first.

**Reference.** [`examples/license-plate-recognition`](../examples/license-plate-recognition/)
(consumes `plate.recognized.v1`, publishes `access.decided.v1`).
[`examples/gate-controller`](../examples/gate-controller/) (acts on
`access.decided.v1`, and only on a literal `allow`).
[`examples/guard-scan-compliance`](../examples/guard-scan-compliance/)
(publishes `screening.completed.v1`).

**Check.** Review.

## Rule 6 — Work only on the cameras selected for the app

**Rule.** Act on the cameras an operator selected for the app and on no
others. An empty selection means do nothing.

**Why.** A costly skill runs on a camera only because an app that
brings it was pointed at that camera, so a thirty-camera site does not
pay for inference on thirty cameras to watch one gate
([CAMERA_ASSIGNMENTS.md](CAMERA_ASSIGNMENTS.md), [ENRICHMENT.md](ENRICHMENT.md)).
An app that works on every camera it can see breaks that.

**How.** `Detector` drops events from unselected cameras before
`on_detections`, and `FrameApp` polls only the selected ones; do not
work around either. An app that manages its own workers reads
`nvr.roster()`: `[]` means stop everything, `None` means core could not
be asked, so keep what is running. Override `on_cameras_update` to
react when the selection changes. An app that reads no camera data
declares `camera_picker=False`.

**Reference.** [`examples/guard-scan-compliance`](../examples/guard-scan-compliance/)
(`_reconcile_roster`: `None` keeps the workers, `[]` stops them).
[`examples/license-plate-recognition`](../examples/license-plate-recognition/)
(`camera_picked()` fails closed until the selection has arrived).

**Check.** Built into the SDK archetypes.

## Rule 7 — "Could not ask" is not "no"

**Rule.** A read that returns `None` means the platform could not be
asked. Never report it as an empty result, and never probe the platform
per frame or per event.

**Why.** An app that treats an outage as "nothing there" tells an
operator that no vehicle is inside, or that no red van came past, when
the truth is that nobody looked. And a probe per event turns a cheap
question into load: the platform itself caches its view of KAI-C for
this reason ([KAI_C_USAGE.md](KAI_C_USAGE.md) §4).

**How.** Keep the last good answer for as long as it is plausible, and
say when it is stale. Cache `ai.capabilities()` and the roster for
minutes, not seconds, and refresh on `on_cameras_update` and on a
timer.

**Reference.** [`examples/license-plate-recognition`](../examples/license-plate-recognition/)
(`_inside_now`: asks core at most once a minute, reuses the last answer
through an outage, and never treats `None` as an empty yard).
[`examples/package-delivery`](../examples/package-delivery/) (the
capability probe is kept for five minutes).

**Check.** Review.

## Rule 8 — Bound every call, back off, and yield

**Rule.** Every call has a timeout. A failure is followed by a growing
wait, not an immediate retry. Background work stops while a person is
waiting for an answer.

**Why.** The platform protects itself: each adapter takes a fixed
number of calls at once, a short line may wait, the next caller is
dropped, and five timeouts in a row pause the adapter for a minute and
longer ([ENRICHMENT.md](ENRICHMENT.md), "When the box cannot keep up").
An app that retries at once into a paused adapter keeps it paused. A
timeout is also not a cancellation: the model usually finishes the work
after the caller has given up, so a retry doubles the load.

**How.** One model call in flight per app unless there is a reason for
more. Back off on failure (the guard-scan app waits two seconds times
the failure count, up to thirty). Show the degraded state on `/health`
instead of failing silently. Drop work that is no longer useful: a
check for a track that has left, a second request for the answer
already on its way.

**Reference.** [`examples/guard-scan-compliance`](../examples/guard-scan-compliance/)
(back-off after repeated failures, and `not_ready_reason` on the
status page). [`examples/ppe-check`](../examples/ppe-check/) (one check
at a time; a queued check for a track that has gone is skipped).

**Check.** Review.

## Rule 9 — Keep state, photos and findings in the platform

**Rule.** Durable state goes in `nvr.state`, photos in the evidence
store, and what the app learns about a visit on the visit. No private
database, no image bytes on the bus, no parallel visit log.

**Why.** A cooldown kept in memory is forgotten on restart, and the app
alerts again for the same thing. An alert is a bus message with a 1 MB
ceiling, so a photo inside it is dropped or truncated; a path is a few
bytes and the picture is fetched once by whoever shows it. A private
log of who came to the door is a second copy of the timeline that
search, journeys and the agent cannot see
([RFC-0003](rfcs/RFC-0003-attaching-what-an-app-learns.md)).

**How.**

```python
nvr.state.set("alerted", sorted(alerted))                 # survives a restart
path = nvr.save_evidence(jpeg)                             # content-addressed path
alert = Alert(..., images={"snapshot": path})              # a path, never bytes
visit = nvr.timeline.visit_at(camera, at, label="person")  # which visit was this?
nvr.timeline.add_claims(visit["event_id"], claims)         # attach what was learned
```

`KeyedState` (or `Detector.keyed_state(ttl)`) is right for per-track
memory that should expire: dwell, last position, a per-object cooldown.

**Reference.** [`examples/line-crossing`](../examples/line-crossing/)
(`_evidence`: snapshot, `save_evidence`, the path in the alert).
[`examples/license-plate-recognition`](../examples/license-plate-recognition/)
(the "already announced" set is in `nvr.state`).
[`examples/smart-doorbell`](../examples/smart-doorbell/)
(`visit_at` then `add_claims`, and no claim is written when the frame
does not identify one visit).
[`examples/loitering-detection`](../examples/loitering-detection/)
(per-track dwell in `keyed_state`).

**Check.** Review.

## Rule 10 — Declare what the app needs

**Rule.** Say in the manifest which tasks the app needs, which extra
classes Tier-0 should track for it, which hosts it reaches outside the
site, and whether it reads camera data.

**Why.** The platform decides from the manifest what to run and where.
`requires_tasks` greys the app out when no installed adapter can serve
it, and gives the catalog the hardware bar to show before install.
`tier0_labels` widens the detector only on the cameras selected for the
app. An app that needs something it did not declare fails with no error
anywhere: it simply sees nothing.

**How.** `requires_tasks` with canonical task names
([`server/config/tasks.yml`](../server/config/tasks.yml)),
`tier0_labels` in lowercase, `network_egress` in the listing (`[]` when
the app never leaves the site, [APP_NETWORK.md](APP_NETWORK.md)),
`camera_picker=False` when no camera data is read.

**Reference.** [`examples/package-delivery`](../examples/package-delivery/)
(`requires_tasks`, `tier0_labels`).

**Check.** `opennvr-app validate` (manifest and listing checks).

---

## Checklist

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

## The checks

`opennvr-app validate [path]` reads the app's Python source (never runs
it) and reports, with file and line:

| Check | Fires on | Rule |
|---|---|---|
| `own-decode` | `VideoCapture(...)`, an `rtsp://` address, `ffmpeg` / `ffprobe` / `gst-launch-1.0`, `import av` | 1 |
| `wide-subject` | the subject `opennvr.inference.>` in the code, or in an example config that also consumes Tier-0 | 1 |
| `per-frame-model` | a model call inside `FrameApp.on_frame`; an `InferStream` or `ai.stream()` session | 2 |
| `direct-adapter` | a route ending in `/infer` that is not KAI-C's `/api/v1/infer/...` | 3 |
| `own-model` | importing a model runtime (`torch`, `onnxruntime`, `ultralytics`, `transformers`, …) | 3 |

They are warnings, and `--strict` turns them into a failure. They are
heuristics: a finding is a question for the author. A real exception is
answered in the code, on the flagged line or the one above it, and the
reason is required:

```python
cap = cv2.VideoCapture(path)   # opennvr: allow[own-decode] reads a file the operator uploaded, not a camera
```

A waived finding is listed in the report with its reason, so a reviewer
sees every exception an app takes. Tests, `tools/`, `scripts/` and
`bench/` are not scanned.

## Planned, not yet rules

These are decided in direction and not finished. Do not build on them
as if they were, and do not treat their absence as permission to go
around the platform.

- **One shared "what is on this camera now".** Core has one path for a
  live description: the newest described visit if it is fresh, else one
  gated look shared by every asker
  (`GET /internal/camera-agent/cameras/{id}/description`, RFC-0004 step
  3). The camera agent and the Mirror use it. It is not in the App SDK
  client yet; until it is, an app that needs a live answer makes one
  `nvr.ai.infer` call on a trigger (rule 2) and caches it.
- **Capability tokens.** An adapter will accept a call only with a
  token KAI-C minted, which turns rule 3 from a convention into a
  refusal (RFC-0001, RFC-0004 principle 5).
- **One call for caption and attributes.** Tried and removed: it halved
  the model's time per visit and lost the specific words search depends
  on. The attribute questions are batched into one call; the caption
  stays its own call (`bench/rfc-0004-infer-once-log.md`). An
  optimisation that lowers answer quality is not taken.

## Where the examples stand

Every example was read against these rules on 2026-11-15. The apps
named under **Reference** above do that rule correctly. These do not
yet, and should not be copied for the part named:

| App | What still goes the old way |
|---|---|
| `smart-doorbell` | Face recognition on every polled frame with no Tier-0 trigger; holds the face adapter's address for enrolment; the alert carries the photo as bytes; dedup is in memory. |
| `gate-watch`, `garbage-dumping` | A model call on every poll (30 s and 60 s) whether or not anything is in the scene. |
| `guard-scan-compliance` | Decodes a video-rate stream and runs pose per frame. This is the declared exception of rule 2 (keypoints at ten frames a second), not a pattern. |
| `camera-agent` | Holds adapter addresses for speech-to-text, text-to-speech, faces and the language model; decodes RTSP itself as a fallback; describes a stored visit by calling the model instead of reading its caption; its watches run the detector on a timer; it does not subscribe to `visit.enriched.v1`. The Mirror's person card runs its own model loop. |
| `ppe-check` | Right on rule 2, but fetches the best frame from a pipeline address it holds instead of the SDK's `BestFrameClient`. |
| `abandoned-object` | Works out "stationary" itself instead of reading Tier-0's flag. |
| `occupancy-counting` | Finds its cameras through the older skill discovery, not `roster()`. |
| `footage-search` | The optional query parser calls a language model at its own address (off by default). |
| `inference-listener` | A plumbing demo: it prints every camera's raw events and does not parse Tier-0 tracks. Not a template for an app. |
| `abandoned-object`, `intrusion-detection`, `loitering-detection`, `line-crossing` | The code defaults and `config.example.yml` are the adapter-only mode (wide subject, Tier-0 off), which sees nothing on a stock install. `config.docker.yml` is the right one. |

No example uses `DomainEventSubscriber`, `timeline.evidence` or
`BestFrameClient` yet; the cookbook
([`sdk/opennvr-app-sdk/cookbook/`](../sdk/opennvr-app-sdk/cookbook/))
has one runnable file for each. Three cookbook files
(`03_frame_app.py`, `11_tier0.py`, `14_infer_stream.py`) predate these
rules and show a model call per frame; read them for the API, not for
the pattern.

## Related reading

- [FIRST_DETECTOR.md](FIRST_DETECTOR.md) — scaffold an app and get its rule green.
- [tier0-consumption.md](tier0-consumption.md) — the Tier-0 event shape and its traps.
- [APP_PLATFORM.md](APP_PLATFORM.md), [SDK_REFERENCE.md](SDK_REFERENCE.md) — the client and every exported name.
- [EVENT_CONTRACTS.md](EVENT_CONTRACTS.md) — every event an app may subscribe to.
- [ENRICHMENT.md](ENRICHMENT.md) — what the enrichment tier computes, and how it yields under load.
- [RFC-0004](rfcs/RFC-0004-infer-once-publish-once.md) — infer once, publish once.
- [CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md) — listing an app in the catalog.
