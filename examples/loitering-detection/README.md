# Loitering Detection

Alerts when a tracked person or vehicle stays inside a drawn zone longer
than a dwell threshold — the ATM vestibule after hours, the fire exit,
the loading bay, the forecourt, the stairwell. Escalates if they remain,
alerts sooner after hours, flags gatherings, and keeps dwell history so
the **Loitering** page can show today by the hour and the last week by
the day.

It rides the detection stream the platform already produces (Tier-0):
no extra model, no GPU.

## What you get

| | |
|---|---|
| **Per-object stays** | Dwell is measured per tracked object. Two people taking turns at a door are two stays; one person who leaves and comes back after the grace period is a new stay. |
| **Staged alerts** | `threshold_seconds` raises the `loitering` alert; `escalate_after_seconds` later, if they are still there, `loitering-escalated` one severity step higher. Each stay alerts once per stage. |
| **Time of day** | `active_hours` is when alerts fire. Outside it the app is quiet — or, with `after_hours_threshold_seconds`, alerts sooner (a back door at 02:00 is a stronger signal than at 14:00). Stays are counted all day either way. |
| **Gatherings** | `group_size` + `group_seconds`: a `gathering` alert when that many watched objects dwell together. |
| **Noise controls** | `grace_period_seconds` absorbs detector gaps inside a stay; `min_bbox_height` drops far traffic and birds; `alert_cooldown_seconds` turns a burst into one alert per camera; **Dismiss** on the Loitering page marks a current dweller as known so their stay raises nothing more. |
| **Evidence** | Every alert carries a snapshot from the camera, the track id, the dwell, the threshold that applied, the zone, and the model fingerprint. |
| **History** | Finished stays are published as dwell on the platform's footfall history (`occupancy.footfall.v1`, dwell fields only), which core keeps per camera-hour for 90 days. |
| **Dashboard** | Who is dwelling now with a progress bar toward the threshold, per-camera stays / alerts / longest / average today, a 24-hour strip, a dwell-length histogram, recent stays, and the app's alarms with their snapshots. |

## Install

Pick the app in the installer, or:

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile loitering-detection up -d
```

Then **App Catalog → Loitering Detection → Configure**: tick the cameras,
draw a zone on each (nothing drawn = the whole frame, and the page says
so), set the threshold, save. Everything applies live; nothing restarts.
The **Loitering** page appears under Applications as soon as the app is
enabled.

## Tuning

Start with the threshold that matches the place, then watch the
dwell-length histogram on the Loitering page for a day before tightening.

| Place | `threshold_seconds` | Notes |
|---|---|---|
| Fire exit, loading bay, forecourt | 30–60 | Nobody should be there long. Add `after_hours_threshold_seconds: 15`. |
| Shop front, ATM vestibule | 90–180 | Browsing is normal; sleeping is not. Escalate after 300. |
| Lobby, waiting area, platform | 300–600 | Long stays are the point of the place; alert only on the outliers. |
| Car park (vehicles) | 600+ | Watch `car`/`truck`; raise `grace_period_seconds` — parked cars drop out of detection. |

Two people who should not both be there: `group_size: 2`,
`group_seconds: 30` on the zone at the shutter.

## How it decides

Per `(camera, track_id)` the app keeps a stay: when the object's box
centre first sat inside the zone, and when it was last seen there. A
detection gap up to `grace_period_seconds` keeps the stay; longer, and
the stay is finished (counted, histogrammed, published) and the next
sighting starts a new one. Alert stages latch per stay, so one person
produces at most one `loitering` and one `loitering-escalated`.

Identity is what makes this well-defined, so the stock config consumes
Tier-0 (`consume_tier0: true`, subject `opennvr.inference.tier0.>`),
which tracks. Without a `track_id` the app degrades to one stay per
`(camera, label)` with a one-time warning.

## Standalone

```bash
cp config.example.yml config.yml   # nats_url, cameras with pixel zones
uv sync && uv run loitering-detection --config config.yml
uv run pytest
```

`config.example.yml` documents every key. With `opennvr_url` set and no
`cameras:` list, cameras and zones come from the App Catalog.
