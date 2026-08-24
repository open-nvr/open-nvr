# Alarms

Alarms are high-severity rules: when a target appears on a watched camera
(optionally only within a time window), the alarm **rings** in the UI until a
human acknowledges it. They sit alongside, but are distinct from, monitors:

| Feature   | Monitor (`create_monitor`)            | Alarm (`create_alarm`)                       |
|-----------|----------------------------------------|----------------------------------------------|
| Severity  | informational                          | urgent — rings until acknowledged            |
| Output    | notification / live count              | flashing banner + audible siren in the UI    |
| Use it for| "notify me when…", "count people on…"  | "sound a fire alarm if…", "alarm after 6pm…" |

## Creating alarms

By voice (the agent routes these to `create_alarm`):
- "Sound a fire alarm if you see fire" → `name=Fire, target=fire`
- "Alarm if a person is detected after 6 pm" → `target=person, after=18:00`
- "Alert me loudly if a car enters between 10pm and 6am on all cameras" →
  `target=car, after=22:00, before=06:00, camera_id=all`

Time windows are 24h `HH:MM`, and a window **repeats daily** by nature
(`after`/`before` are times of day). No window at all means **all day,
every day** — the default; nothing about time is required. A window
where `after > before` (e.g. `22:00`–`06:00`) wraps across midnight.
The UI offers the same choices as quick picks: All day (default), Night
(22:00–06:00), After hours (18:00–08:00), or a custom from/until pair.
The agent silences with `stop_alarm` (`action: silence`) or removes with
`stop_alarm` (`action: disarm`) — disarming deletes the alarm from the
list. Arming the exact same alarm twice (same target, cameras, window,
level) is refused with a pointer at the existing one, so a double-click
can never stack duplicates.

The UI also offers one-tap **preset** alarms and an "add alarm" path via
`POST /alarms`. Presets come from `GET /alarm-presets` with **honest,
capability-aware availability**: each preset's target is checked against what
the detection path can actually see (the standard YOLOv8/COCO-80 vocabulary
plus the config's `detector_extra_labels`). Detectable presets — After-hours
person, Person, Car, Truck, Dog — arm in one click; the safety presets (Fire,
Smoke, Gas leak) are greyed out on a stock stack because COCO has no such
classes, and clicking one explains what to register to enable it.

The add-alarm form (the demo's Automations card, + → 🔔 Alarm) makes the
same honesty interactive: its target input is backed by a pick-list of the
install's real detectable vocabulary (`GET /alarm-targets` — common
security labels first), and the camera is an **explicit picker** (all
cameras, or one) — no form arms a camera set by side effect. The same
form, minus the picker, sits on each camera's own screen fixed to that
camera, and presets follow the picker's choice too.

## How it works

`AlarmManager` runs one background loop per alarm. Every few seconds, when the
alarm is within its time window, it grabs a frame from each watched camera,
runs object detection, and counts the target. On a rising edge (target present
and the alarm wasn't already ringing, and the re-arm cooldown has elapsed since
the last acknowledge) it sets `triggered=True` and logs an event. The UI polls
`GET /alarms`, plays a two-tone Web Audio siren and shows a red banner while any
alarm is triggered, and stops on acknowledge.

Detection is periodic-snapshot based (same engine as monitors), so an alarm
can take up to one poll interval to fire — tune `AlarmManager(interval=…)` for
your latency/load needs.

### Endpoints
- `GET /alarms` → `{alarms, events, ringing}`
- `POST /alarms` → arm (`name`, `target`, `camera_id`/`camera_ids`, `after?`, `before?`)
- `POST /alarms/ack` → silence one (`{alarm_id}`) or all (`{}`); keeps it armed
- `DELETE /alarms/{id}` → disarm/remove

## Emergency calling — FUTURE (documented, not yet implemented)

Alarms can be associated with an emergency contact via config:

```yaml
# config.yml
emergency_contacts:
  fire: "+1-555-0100"      # keyed by alarm target or name (case-insensitive)
  person: "+1-555-0199"
```

When an alarm whose `target`/`name` matches a configured contact fires, the
event today records `"would alert <number>"` and surfaces that in the UI — it
does **not** place a call yet.

**Planned integration (not built):** wire the trigger to a telephony provider
(e.g. Twilio Voice / SIP) to place an automated call or SMS to the configured
number with a synthesized message ("Fire detected on the front-porch camera at
14:05"). Design notes for when we implement it:
- Put the provider credentials in server-side config/secrets, never in the
  browser or this example's config.
- Rate-limit and require acknowledge/cancel windows to avoid false-positive
  call-outs (vision detection is not certified life-safety equipment).
- Add an explicit per-alarm `call_on_trigger: true` opt-in and an audit log of
  every call placed.
- **Do not** treat this as a replacement for certified fire/intrusion alarm
  systems — it is an assistive notification layer.
