# Intrusion Detection

The perimeter alarm. A drawn zone is **armed** — on a schedule, around
the clock, or on command — and when a watched class enters it the site
goes to **alarm**: the fence line at night, the yard behind the shutter,
the plant room, the roof, the bay that should be empty after the last
shift.

It is built on the vocabulary every operator already knows from an
intrusion panel, not on a bare time window:

```
disarmed ──arm──▶ arming (exit delay) ──▶ armed
                                            │ watched class in the zone,
                                            │ present min_presence_seconds
                                            ▼
                                       breach (entry delay, counting down)
                                            │ still there when it expires
                                            ▼
                                          alarm ──▶ re-arms after
                                                    alarm_reset_seconds
```

It rides the detection stream the platform already produces (Tier-0):
no extra model, no GPU, and detections arrive at the detector's rate
rather than a poll interval — an intruder who crosses the zone between
two polls is no longer missed.

## What you get

| | |
|---|---|
| **Arming, four ways** | `arm_mode` is `schedule` (armed inside `armed_hours` — the classic restricted hours, cross-midnight allowed), `always`, `manual` (only the buttons move it), or `off` (watch and report, never alarm). |
| **Exit and entry delay** | `exit_delay_seconds` is the grace after arming, time to walk out. `entry_delay_seconds` is the grace between the breach and the alarm, time to be recognised or to disarm — the Perimeter page shows the countdown. Both 0 makes the perimeter instant, which is what a fence line wants. |
| **Presence filter** | `min_presence_seconds` ignores a box that clips the zone edge for a frame. With tracking underneath it, this is what separates a perimeter alarm from a motion sensor. |
| **One intruder, one alarm** | A breach belongs to a tracked object, so a person standing in the zone raises one alarm, not one per frame. `alarm_cooldown_seconds` merges a group coming over the fence into a single alarm per camera. |
| **Escalation** | Still inside `escalate_after_seconds` after the alarm: `intrusion-escalated`, one severity step higher — the "they are not leaving" signal a monitoring desk acts on differently. |
| **Bypass and override** | Bypass a camera for a stated number of minutes (contractors in the yard) — it stays on the page as *bypassed* rather than silently ignored, and un-bypasses itself. A manual arm/disarm holds for `override_minutes` and then the mode takes over again, so "disarm for the delivery" cannot be forgotten forever. |
| **Operator actions** | Arm, Disarm, Bypass, Acknowledge (clear the alarm and re-arm without waiting for the reset timer), and Back to schedule — from the Perimeter page or any API client. |
| **Verification** | Every alarm carries a snapshot from the camera, the track id, the class, how long they had been inside, the zone, and the model fingerprint. |
| **Dashboard** | Cameras armed, cameras in alarm, breaches and alarms today, a per-camera state pill with its countdown, who is inside right now with their stage, and the app's alarms with their snapshots. |

## Install

Pick the app in the installer, or:

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile intrusion-detection up -d
```

Then **App Catalog → Intrusion Detection → Configure**: tick the
cameras, draw the zone on each (nothing drawn = the whole frame, and the
page says so), choose the arm mode and hours, save. Everything applies
live; nothing restarts. The **Perimeter** page appears under
Applications as soon as the app is enabled.

## Tuning

| Place | Settings | Notes |
|---|---|---|
| Fence line, roof, plant room | `exit_delay: 0`, `entry_delay: 0`, `min_presence_seconds: 1` | Nobody arrives through a door. Instant alarm. |
| Yard behind the shutter | `arm_mode: schedule`, `armed_hours: 19:00–07:00`, `entry_delay: 20` | Twenty seconds for the late shift to be recognised or to disarm. |
| Loading bay, back office | `exit_delay: 45`, `entry_delay: 30`, `override_minutes: 60` | The panel pattern: arm on the way out, disarm on the way in. |
| Long perimeter with a road behind it | `min_bbox_height: 0.1`, `watch_labels: [person]` | Drops far traffic and birds before anything else runs. |

Start with `min_presence_seconds` if you are getting false alarms and
`entry_delay_seconds` if you are getting true ones you would rather
handle yourself. Watch a night on `arm_mode: off` first — the page still
counts breaches, so you can see what *would* have alarmed before you
arm anything.

## How it decides

Each camera holds one state. `tick()` runs every second on the wall
clock and advances it: exit delays expire into `armed`, entry delays
expire into `alarm`, an alarm escalates, a zone clear for
`alarm_reset_seconds` returns to `armed`, bypasses and manual overrides
expire on their own.

Detections come in per `(camera, track_id)`. An object whose box centre
sits inside the zone starts a presence clock; once it passes
`min_presence_seconds` on an armed camera it is a breach, and the breach
belongs to that track until it leaves. Tier-0 sends nothing for frames
with no detections, so an emptying zone is silent — the wall-clock sweep
in `tick()` is what notices the last intruder left, after
`track_ttl_seconds`.

Identity is what makes one-alarm-per-intruder well-defined, so the stock
config consumes Tier-0 (`consume_tier0: true`, subject
`opennvr.inference.tier0.>`), which tracks. Without a `track_id` the app
degrades to one presence per `(camera, label)` with a one-time warning.

## Standalone

```bash
cp config.example.yml config.yml   # nats_url, cameras with pixel zones
uv sync && uv run intrusion-detection --config config.yml
uv run pytest
```

`config.example.yml` documents every key. With `opennvr_url` set and no
`cameras:` list, cameras and zones come from the App Catalog.
