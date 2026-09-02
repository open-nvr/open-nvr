# Consuming Tier-0 from an app

A default OpenNVR install runs the **detect-pipeline** (Tier-0) and no
per-frame adapter loop. That makes Tier-0 the *only* detection stream on
the bus out of the box — so an app that ignores it sees nothing, forever,
on a stock system. This page is how an app consumes it.

## The two event shapes

Adapter events (what apps were originally written for) and Tier-0 events
are deliberately different, and the difference is why an app can look
"installed and healthy" while doing nothing:

```jsonc
// adapter, e.g. opennvr.inference.yolov8.cam1.completed
{ "camera_id": "cam1", "completed_at": "2026-08-21T05:16:08Z",
  "result": { "detections": [ { "label": "person",
                                "bbox": {"x":0.4,"y":0.4,"w":0.1,"h":0.1} } ] } }

// Tier-0, opennvr.inference.tier0.cam1.completed
{ "schema": "opennvr.tier0.v1", "adapter": "tier0", "camera_id": "cam1",
  "seq": 41, "ts": 1234.5, "wall_ts": 1755700000.0,
  "frame": { "w": 1920, "h": 1080 }, "calibrating": false,
  "tracks": [ { "id": 3, "label": "person", "score": 0.91,
                "box": [768, 432, 960, 540],     // PIXELS, not normalised
                "stationary": false, "best": true } ] }
```

Three traps live in that payload:

* **`tracks`, not `result.detections`.** There is no `result` block at all.
* **Pixel boxes.** `box` is `[x1, y1, x2, y2]` in frame pixels; the
  contract's `bbox` is normalised 0–1. `frame.w/h` is there to convert.
* **`ts` is `time.monotonic()`, not a date.** It counts from an arbitrary
  origin (typically boot). Store it as a timestamp and every record lands
  near 1970. Use **`wall_ts`** (epoch seconds) for anything time-related.

## Detector apps: one flag

For apps built on the SDK's `Detector`, the bridge is built in — turn it
on and `on_detections` receives normal contract-shaped detections
(`label`, `score`, `bbox`, plus `track_id`, `stationary`, `best`):

```yaml
# config.yml
subject_pattern: "opennvr.inference.tier0.>"   # Tier-0 only
consume_tier0: true
```

Add the field to the app's config dataclass and parse it (the SDK reads
`getattr(config, "consume_tier0", False)`, so a flag the config object
doesn't carry is silently ignored):

```python
@dataclass
class AppConfig:
    ...
    consume_tier0: bool = True

# in load_config(...)
consume_tier0=bool(raw.get("consume_tier0", True)),
```

**Why the narrow subject.** With `opennvr.inference.>` *and* the flag on,
an app that later gains a heavy adapter processes the same object twice —
once from Tier-0, once from the adapter — and fires double alerts.
Subscribing to `opennvr.inference.tier0.>` makes that impossible by
construction. Widen it (and accept the dedup problem) only when you
actually want adapter events too.

## Whole-event apps

An app that overrides `handle_event` (an indexer, a relay) bypasses the
flag entirely and receives Tier-0 events whether or not it understands
them — so it must parse `tracks` itself:

```python
labels = [d["label"] for d in (event.get("result") or {}).get("detections", [])]
if not labels:                                  # Tier-0 shape
    labels = list(dict.fromkeys(
        str(t["label"]) for t in event.get("tracks") or [] if t.get("label")))
```

Use `opennvr_app_sdk.tier0.tier0_to_detections(event)` instead of
hand-rolling the bbox maths — it does the pixel→normalised conversion and
skips malformed tracks.

## What Tier-0 gives you for free

Beyond costing nothing (one detector, N subscribers — versus every app
driving its own inference stream):

* **`track_id`** — stable across frames, so "the same person" is free.
* **`stationary`** — the object has held still for 50 frames (~25 s at
  the default `DETECT_FPS=2`; it counts frames, not seconds). Dwell and
  abandoned-object rules can read it instead of re-deriving it.
* **`best: true`** — Tier-0 retained the sharpest crop for that track and
  serves it at `<pipeline>/best_frame?camera=&track=`. Run an expensive
  model on *that*, never an arbitrary live grab.

## Checklist when an app indexes/alerts nothing

1. Is `consume_tier0` on the config **object**, not just the YAML?
2. Does `subject_pattern` match `opennvr.inference.tier0.>`?
3. Do the configured `camera_id`s match what Tier-0 publishes? The
   classic failure is `cam-1` in config vs `cam1` on the bus — apps
   should log once per unknown camera id rather than counting nothing in
   silence.
4. Is anything being published at all? Tier-0 skips empty frames by
   default, so a still scene is legitimately quiet:
   `docker compose logs detect-pipeline | grep published`.
