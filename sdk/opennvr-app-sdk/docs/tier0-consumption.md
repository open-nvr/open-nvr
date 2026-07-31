# Consuming Tier-0 in your app — answer from the always-on detector, reuse its best frame

OpenNVR's **Tier-0** detect-pipeline runs cheap detection full-time and publishes
the current tracks, per frame, to `opennvr.inference.tier0.<camera>.completed`. Your
app usually does **not** need to run its own expensive model for the common cases —
it can read what Tier-0 already produced. This is the same "the bus *is* the sharing
mechanism" principle the platform is built on: there is **no new contract**, just an
event to read and a best frame to fetch.

This is **opt-in**. Use it where it fits your app's semantics; ignore it where it
doesn't (see [When *not* to use it](#when-not-to-use-it)). Nothing here is mandatory,
and an app that doesn't configure it behaves exactly as before.

## The two things Tier-0 gives you

**1. Metadata — counts and presence, for free.** "How many cars?", "is anyone at the
door?", "is there a package?" are already answered by the latest event's `tracks`.
Read them; run no inference.

**2. The best frame — for when you *do* need a model.** Appearance questions ("what
colour is the car?", "what is the person wearing?") still need a vision model — but
it should run on Tier-0's **best frame**, the sharpest / largest / most-confident
frame Tier-0 already selected for that track, not an arbitrary live grab. That is
more accurate (a clean frame, not a blurred/backlit one) and cheaper (one inference
on the right frame). Tier-0 retains that crop and serves it at
`<pipeline>/best_frame?camera=&track=`; each track in the event carries a `best`
flag when one is available.

## The event shape (`opennvr.tier0.v1`)

```jsonc
{
  "schema": "opennvr.tier0.v1",
  "adapter": "tier0",
  "camera_id": "front",
  "seq": 128, "ts": 17532.4,
  "tracks": [
    { "id": 1, "label": "person", "score": 0.91, "box": [x1,y1,x2,y2],
      "stationary": false, "best": true }
  ]
}
```

`best: true` means a best-frame crop is fetchable for that track at
`GET <pipeline_metrics_origin>/best_frame?camera=<camera>&track=<id>` (JPEG, `404`
if none yet). Omit `&track=` to get the camera's most-recent best frame.

## Using the SDK helpers

The App SDK provides both primitives so you don't re-implement them:

```python
from opennvr_app_sdk import snapshot_from_event, BestFrameClient

# 1) Metadata — parse a Tier-0 event you received on the bus:
snap = snapshot_from_event(event_payload)   # event_payload = decoded JSON dict
snap.counts            # {"person": 1, "car": 2}
snap.present("person") # True
snap.count("car")      # 2
snap.describe()        # "a person, 2 cars"  (speakable)
snap.has_best(1)       # a best frame is fetchable for track 1

# 2) Best frame — fetch it when you actually need to run a vision model:
best = BestFrameClient("http://tier0:9109")          # the pipeline metrics origin
jpeg = await best.fetch("front", track_id=1)         # bytes, or None
if jpeg is None:
    jpeg = await get_live_frame("front")             # graceful fallback
answer = await my_vlm.infer(frame_jpeg=jpeg, question="what colour is the car?")
```

`BestFrameClient` takes an optional `resolve_camera` to map your app's camera id →
the pipeline's camera id (the id on the Tier-0 subject), and an injectable
`http_get` for tests. `make_best_frame_fetch(...)` returns a bare
`async fetch(camera_id) -> bytes | None` if that shape is easier to plug in.

A `Detector`-archetype app already receives `opennvr.inference.*` events, so
`snapshot_from_event` slots straight into its handler; a `FrameApp` that drives its
own inference can call `BestFrameClient` before deciding to fetch a fresh frame.

## The decision rule

| Question is about… | Do this |
|---|---|
| **count / presence** ("how many", "is X there") | read `snapshot_from_event` — **no inference** |
| **appearance of one object** (colour, clothing, damage, plate) | run your model on `BestFrameClient.fetch(cam, track)` |
| **the whole scene / context** ("what's happening") | run your model on the **full live frame** |
| **right-now liveness** ("is it open *now*") | use a **live** frame, not the (slightly stale) best frame |

## When *not* to use it

- **Liveness-critical** checks need the *current* moment — the best frame may be a
  few seconds old (it's the best over the track's recent life). Fetch a live frame.
- **Whole-scene** questions need the full frame, not a single-track crop.
- **No-vision apps** (audio, LLM, relays) don't touch frames — this is irrelevant.
- If Tier-0 isn't running for a camera (`analyze=false`, or the pipeline is off),
  `latest_inference` / `fetch` return nothing — always keep a graceful fallback.

## Reference example

The camera-agent (`examples/camera-agent`) uses both: its `camera_snapshot` tool
answers counts/presence from `snapshot_from_event` with no inference, and
`describe_camera` runs the VLM on `BestFrameClient`'s best frame (configured via
`bestframe_base_url`), falling back to a live grab when none is available.

## Zero-code path for detector apps: `consume_tier0`

Every `DetectorApp` can consume Tier-0 without new code. Set `consume_tier0: true`
in the app's config and the SDK bridges each Tier-0 event's `tracks` into
contract-shaped detections (`tier0_to_detections`) — your existing
`on_detections(camera_id, detections, event)` runs unchanged, with
`det["bbox"]` as the usual NormalizedBBox (computed from the event's `frame`
size) plus Tier-0 extras: `track_id`, `stationary`, `best`.

It is **off by default** on purpose: an app also subscribed to a heavy adapter
(e.g. yolov8) would otherwise see the same object twice and double-alert. Turn
it on when Tier-0 *replaces* the heavy adapter for your app, or when you filter
by adapter in `on_detections`. When off, Tier-0 events are ignored **and not
counted** toward `/health` `events_seen` / `last_event_age_s`, so the
per-frame Tier-0 stream can never mask a stalled adapter.
