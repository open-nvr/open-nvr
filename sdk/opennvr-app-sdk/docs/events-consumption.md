# Reading the platform's memory — the events store from an app or agent

OpenNVR keeps a canonical history: **one row per object visit** (person, car,
dog — whatever the detector emits), each with the **best photo** Tier-0 saw of
it, plus (in later phases) camera alarms and app incidents on the same
timeline. Your app should **query this instead of keeping its own history
store** — persistence is the platform's job now.

## The SDK client

```python
from opennvr_app_sdk import EventsClient

events = EventsClient(core_url, internal_api_key)

# "Which cars entered between 3 and 4pm?"
visits = await events.search(label="car",
                             start="2026-08-12T15:00", end="2026-08-12T16:00")
for v in visits:
    print(v.camera_id, v.started_at, v.ended_at, v.score, v.has_evidence)

# The visit's best-frame JPEG (input for face match / LPR / VLM):
jpeg = await events.evidence(visits[0].id)
```

Semantics you can rely on:

- **Overlap, not containment:** a visit that started 14:58 and left 15:03 IS
  returned for a 15:00–16:00 window — that's what the human question means.
- **Newest first**, `limit` capped server-side.
- **Degrades to empty** on any transport error — a memory lookup failing
  should soften an answer, never crash an app.
- Labels are lowercase; pass `camera_id` as the server-side camera id.

## When to use which history mechanism

| You want | Use |
|---|---|
| "What happened between X and Y?" with photos | `EventsClient.search` (this doc) |
| Live "something is happening now" | subscribe the bus (`opennvr.inference.>`) |
| The best frame of a track that is STILL live | `BestFrameClient` (tier0-consumption.md) |

The camera-agent's `search_history` tool is built on exactly this client —
including face-matching the returned evidence crops via the recognition
adapter. Read it (`examples/camera-agent/tools.py`) as the reference consumer.
