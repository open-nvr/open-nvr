# Abandoned Object

A bag on the concourse, a box against a fire door, a trolley in the
aisle, a case at the platform edge. The question is never "is there a
bag?" — it is **is anyone with it, how long has it been alone, and who
put it there?** So this app follows each item through a life rather
than firing a bare alarm:

```
moving ──settles──▶ with owner ──they walk away──▶ unattended
                        ▲                              │
                        │                       unattended_seconds
                  someone returns                      ▼
                        └──────── reclaimed ◀──── abandoned ──▶ escalated
```

It rides the detection stream the platform already produces (Tier-0):
no extra model, no GPU.

## What you get

| | |
|---|---|
| **Who left it** | When an item settles, the nearest person is remembered — their track and the moment they walked off. The alert reads "alone for 74s; the person who left it was track 312", which is a description a guard can act on rather than a bare alarm. An item that appears with nobody near it says that instead. |
| **Attendance, not presence** | An item is attended while *anyone* is within `owner_radius` of it, and `owner_grace_seconds` absorbs the detector losing that person for a frame. The clock only starts once nobody has been near it — the single most effective false-alarm filter here, because a bag beside its owner is the normal case. |
| **Settling** | `settle_seconds` of near-stationary tracking before an item is a candidate at all, and `move_tolerance` decides "still". Anything merely carried past the camera never enters the list. |
| **Reclaimed closes the loop** | If the item moves again, or someone comes back for it, the app says so and drops it; an abandoned item that vanishes is reported as taken or hidden. The page tells "still there" from "gone" — the thing an unattended-baggage panel usually leaves you guessing about. |
| **Fixtures** | A bin, a planter, a pallet looks like an abandoned box forever. **Mark as fixture** records that spot and stops it alerting there, so the scene's furniture is silenced in one click instead of by loosening the threshold everywhere. Permanent ones go in `fixtures:`. |
| **Escalation** | Still there, still unacknowledged after `escalate_after_seconds`: `abandoned-object-escalated`, one severity step higher. An acknowledged item stops escalating but stays on the page. |
| **Noise controls** | `min_bbox_height` / `max_bbox_height` drop litter and vehicles; `alert_cooldown_seconds` turns a pile of bags into one alert per camera; `active_hours` keeps a shop quiet by day while still counting. |
| **Evidence** | Every alert carries a snapshot, the class, the track, the seconds alone, the owner track and when they left, the zone and the model fingerprint. |
| **Dashboard** | What is on the floor now with a progress bar to the threshold and who left it, per-camera items / alerts / reclaimed today, the fixture count, recent events, and the app's alarms with their snapshots. |

## Install

Pick the app in the installer, or:

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile abandoned-object up -d
```

Then **App Catalog → Abandoned Object → Configure**: tick the cameras,
draw a zone on each (nothing drawn = the whole frame, and the page says
so), set the threshold, save. Everything applies live; nothing restarts.
The **Left Items** page appears under Applications as soon as the app is
enabled.

## Tuning

Thresholds belong to the place, not the product:

| Place | `unattended_seconds` | Notes |
|---|---|---|
| Boarding gate, platform edge | 45–60 | The classic unattended-baggage window. Escalate after 300. |
| Check-in hall, ticket line | 90–120 | People put bags down constantly; `owner_radius` matters more than the clock. |
| Seating area, food court | 180 | Long stays are normal. Raise `owner_radius` so the next table is not read as abandonment. |
| Loading bay, back corridor | 60 | Anything left is in the way. Add `active_hours` if the day shift stages goods there. |
| Long-stay car park | 600+ | Watch `suitcase`/`box`; raise `track_ttl_seconds` for items that drop out of detection. |

Start with `owner_radius` if you are getting false alerts (it is almost
always attendance, not the clock), and with `settle_seconds` if items
flicker in and out of the list. Run a day with `escalate_after_seconds:
0` and read the recent log before turning escalation on.

## How it decides

Per `(camera, track_id)` the app keeps the item's life: where it
settled, when, who was nearest then, when anybody was last within
`owner_radius`, and its state. `tick()` runs every second on the wall
clock and advances the unattended clocks, the escalations, and items
nobody has seen for `track_ttl_seconds`. Distances are fractions of the
frame width, so a threshold tuned on one camera means the same on the
next whatever its resolution.

Identity is what makes "this bag has been alone for ninety seconds"
well-defined, so the stock config consumes Tier-0 (`consume_tier0:
true`, subject `opennvr.inference.tier0.>`), which tracks. Without a
`track_id` an item cannot be followed, so nothing is reported and the
app warns once.

Upgrading from 1.0: `dwell_seconds`, `move_tolerance_px` and
`person_radius_px` still load — the two pixel knobs are read as
fractions of a 1920-wide frame — but the new names are the ones the
catalog edits.

## Standalone

```bash
cp config.example.yml config.yml   # nats_url, cameras with pixel zones
uv sync && uv run abandoned-object --config config.yml
uv run pytest
```

`config.example.yml` documents every key. With `opennvr_url` set and no
`cameras:` list, cameras and zones come from the App Catalog.
