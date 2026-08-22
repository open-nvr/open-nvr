# occupancy-counting

**Count how many people (or vehicles) are inside a zone, and alert when
it gets too crowded — or too empty.**

A NATS-subscribing monitoring app. It rides the inference stream another
app (e.g. [`intrusion-detection`](../intrusion-detection)) is already
driving, counts the watched-label detections whose bbox center falls
inside each operator-defined zone, and fires an **edge-triggered** alert
when a zone crosses an occupancy threshold. Because it subscribes rather
than drives, it pays **zero adapter/GPU cost** on top of whatever
detection is already running.

| | |
|---|---|
| Pattern | Subscribes to NATS inference events → fires alerts |
| Adapter | (rides upstream's YOLOv8 — no direct adapter call) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | Edge-triggered state machines, zone counting, threshold debounce |

## What it does

For every inference frame, for every configured camera × zone, it:

1. counts detections whose label is in `watch_labels` and whose bbox
   center is inside the zone polygon;
2. classifies the count into a band — `over` (`> max_occupancy`),
   `under` (`< min_occupancy`, if configured), or `normal`;
3. fires an alert **only on a band transition**, so a crowded room
   emits one alert, not one per frame.

Use it for room/venue capacity, queue and concourse crowding, loading-bay
vehicle limits, or a guard post that must always be staffed
(`min_occupancy: 1`).

## Per-camera assignment

Leave `cameras: []` and the app watches every camera OpenNVR knows
about — unless the camera settings page assigns the
`occupancy_counting` skill to specific cameras, in which case the app
scopes to exactly those (re-checked every discovery refresh, no
restart). No assignment anywhere = no restriction. See
[docs/CAMERA_ASSIGNMENTS.md](../../docs/CAMERA_ASSIGNMENTS.md).

## Run it

```bash
cd examples/occupancy-counting && uv sync --extra dev
cp config.example.yml config.yml      # edit camera URLs, zones, thresholds
python occupancy_counting.py --config config.yml
```

Smoke-test against a single event then exit:

```bash
python occupancy_counting.py --config config.yml --once
```

## Configure

Everything an operator touches is in [`config.example.yml`](config.example.yml).
The key knobs:

- **`watch_labels`** — what to count (`["person"]`, or vehicle classes).
- **`max_occupancy`** / **`min_occupancy`** — the band edges. Set app-level
  defaults and/or override per camera. `min_occupancy` is optional.
- **`debounce_frames`** — how many consecutive frames the new band must
  persist before firing (raise it to ignore single noisy frames).
- **`clear_alerts`** — also emit a low-severity alert when a zone returns
  to normal (off by default).

## How alerts flow

Alerts always print to stdout, and optionally POST to a `webhook_url` and
publish to NATS under
`opennvr.alerts.app.occupancy-counting.{camera_id}` — the same §11.5 wire
shape every example emits, so [`alerts-subscriber`](../alerts-subscriber)
and [`home-assistant-relay`](../home-assistant-relay) consume them with no
extra wiring.

## What it does NOT do (yet)

- **No per-track identity.** It counts boxes per frame, so two people
  standing close enough to merge under upstream NMS count as one. Pair an
  upstream tracker (`bytetrack`) if you need identity-stable counts.
- **No time-of-day schedules.** Thresholds are constant. Wrap it or extend
  the config if "max 4 after hours, max 40 during the day" matters.
- **No cumulative footfall.** This is instantaneous occupancy, not an
  in/out turnstile total — see [`line-crossing`](../line-crossing) for
  directional counting.

## Tests

```bash
uv run pytest          # or: PYTHONPATH=. python -m pytest tests/ -q
```

The suite drives `handle_event` with synthetic events and asserts the
state machine fires on transitions (not every frame), honours debounce,
and handles under-occupancy and the clear path.
