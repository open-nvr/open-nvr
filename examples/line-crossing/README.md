# line-crossing (tripwire)

**Fire an alert when a tracked person or vehicle crosses a line in a
chosen direction.** Perimeter tripwire, directional entry/exit counter,
one-way corridor enforcement, loading-dock gate traffic.

A NATS-subscribing monitoring app. Unlike the zone-based examples, a
tripwire needs **per-object identity** — it has to know the *same* object
moved from one side of the line to the other. The stack's always-on
Tier-0 detector tracks natively, so the compose config rides it
(`consume_tier0: true`) at zero extra inference cost; a chained
`bytetrack` adapter is the alternative for a custom detector.

| | |
|---|---|
| Pattern | Subscribes to NATS inference events (tracked) → fires alerts |
| Adapter | (rides upstream's detector + `bytetrack` — no direct call) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | Per-track state, directional segment-crossing geometry |

## What it does

Per `(camera, tripwire, track_id)` it remembers the track's previous
center point. When the next center arrives, it tests whether the segment
`previous → current` crosses the tripwire **and** flips to the other
side. If it does, and the direction matches the wire's `count_direction`,
it fires once for that crossing. Idle tracks are forgotten after
`track_ttl_seconds` so memory stays bounded.

**Direction convention:** the tripwire is an oriented segment A→B. An
object starting on the *left* of the A→B vector and ending on the right
is `a_to_b`; the reverse is `b_to_a`. Set `count_direction` to `a_to_b`,
`b_to_a`, or `both`. Unsure which way is which? Run with `both`, read the
`direction` field on the alerts, then pin it down.

## Run it

```bash
cd examples/line-crossing && uv sync --extra dev
cp config.example.yml config.yml      # edit camera URLs, line endpoints, direction
python line_crossing.py --config config.yml
```

> **Wire up tracking first.** Without `track_id` on detections this app
> can't define a crossing — it logs a one-time warning and ignores
> untracked detections. Chain the `bytetrack` adapter after your detector
> so events carry stable track IDs.

## Counting

Every crossing is tallied — per camera, per direction, since start,
for **today** (which starts over at `daily_reset_hour`), and per hour
for the last 24 h. Call the directions what they mean with
`label_a_to_b` / `label_b_to_a` (`in` / `out` by default; `north` /
`south`, `entering` / `leaving` — whatever the guard says).

Counts are published every `footfall_period_seconds` as
`occupancy.footfall.v1` domain events (a→b as entries, b→a as exits),
which core already sums into 90-day per-camera-hour history and serves
at `GET /api/v1/occupancy/footfall`. The **Tripwires** page charts last
week from that; this app never has to remember it. Turn
`publish_footfall` off if the occupancy app has an entry line on the
same camera, or the two will be summed.

## Alerting

Counting always runs; `alert_mode` says what a crossing does beyond it:

| `alert_mode` | Behaviour | Use it for |
|---|---|---|
| `every` | one alert per crossing | a perimeter, a fence line |
| `threshold` | an alert each time today's count reaches a multiple of `passthrough_threshold` | "tell me at the 100th visitor" |
| `off` | count only | a footfall counter |

On top of that: `active_hours` (alerts only inside a daily window, e.g.
`22:00`–`06:00`; counting is unaffected), `alert_cooldown_seconds` per
camera (a group at the gate is one alert, every person is still
counted), `alert_severity`, and `attach_snapshot` — a still from the
camera stored as evidence and cited by the alert.

## Filters

A tripwire on a real camera sees more than what you want to count:

- **`watch_labels`** — `person`, or `car`/`truck` for a vehicle gate.
- **`min_track_age_seconds`** — a track that flickered into existence
  on the line a moment ago is not a crossing.
- **`min_bbox_height`** — objects shorter than this fraction of the
  frame (birds, far traffic) do not count.

## Cameras and lines

With no `cameras:` in the config the app counts on exactly the cameras
**picked** for it (App Catalog → Line Crossing → Configure → Cameras),
follows pick changes live, and takes each camera's line from the App
Catalog's tripwire editor — drawn on the real scene, applied live.
Nothing picked means nothing counted and no compute. A picked camera
without a line is shown as *not drawn* on the dashboard rather than
counting nothing in silence.
Listing cameras explicitly (see `config.example.yml`) pins the set.

Every knob above is a manifest param: edit it in the catalog's config
form and it applies without a restart.

## The Tripwires page

Enabling the app lights **Applications → Tripwires**: per camera, today's
in / out / net with a 24-hour bar strip, last week from the platform's
footfall history, the recent crossings with their snapshots, and the
cameras still waiting for a line.

## How alerts flow

Same §11.5 wire shape as every example — stdout always, plus optional
webhook and NATS publish to
`opennvr.alerts.app.line-crossing.{camera_id}`, consumed by
[`alerts-subscriber`](../alerts-subscriber) and
[`home-assistant-relay`](../home-assistant-relay) unchanged.

## What it does NOT do (yet)

- **One line per camera.** The catalog's editor draws one tripwire per
  camera per app. Two counted doors on one camera means two cameras
  today, or one line placed to cut both paths.
- **No multi-segment polylines.** One straight segment per wire. Model a
  jagged boundary as several cameras/wires, or extend `line.py`.
- **No re-identification across cameras.** Track IDs are per camera; a
  person leaving cam-A and entering cam-B is two tracks.

## Tests

```bash
uv run pytest          # or: PYTHONPATH=. python -m pytest tests/ -q
```

Covers the directional crossing geometry (including the `count_direction`
filter and the grazing-the-line edge case) and the per-track
`handle_event` state machine.
