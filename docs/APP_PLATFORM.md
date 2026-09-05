# The app platform client — `opennvr_app_sdk.OpenNVR`

**Status:** shipped (SDK ≥ 0.3.0, server `api_version` 1.2; async client SDK ≥ 0.4.0).

Everything a vision app needs from the platform, behind one object that
already holds the app's credential ([APP_CREDENTIALS.md](APP_CREDENTIALS.md))
and therefore the app's camera roster. Nothing an app does with core,
KAI-C or the event bus needs a hand-written HTTP or NATS client any more.

```python
from opennvr_app_sdk import OpenNVR

nvr = OpenNVR()                                   # OPENNVR_URL + app key

for cam in nvr.cameras():                         # the cameras assigned to this app
    jpeg = nvr.snapshot(cam)                      # current frame, or None
    result = nvr.ai.infer("yolov8", jpeg, task="object_detection",
                          camera_id=cam.handle)   # KAI-C, HTTP
    with nvr.ai.stream("yolov8", camera_id=cam.handle) as session:
        result = session.infer(jpeg)              # KAI-C, persistent WebSocket

nvr.state.set("last_plate", "KA01AB1234")         # durable, survives restarts
last = nvr.state.get("last_plate")

for seg in nvr.recordings(cam).list(start=...): ...
clip = nvr.recordings(cam).url(seg.start, seg.duration)
still = nvr.recordings(cam).frame_at("2026-09-05T10:00:00Z")

visits = nvr.timeline.search(camera=cam, label="car", start=...)
photo  = nvr.timeline.evidence(visits[0]["id"])
stats  = nvr.timeline.plate_stats(days=7)

mine   = nvr.alerts.inbox(unacked=True)           # what the operator hasn't acked
caps   = nvr.ai.capabilities()                    # adapters, tasks, health
```

## Surface

| Method | Core / KAI-C route | Scoped to |
|---|---|---|
| `cameras()`, `camera(x)` | `GET /internal/camera-agent/cameras` | the app's roster |
| `snapshot(cam)` | `GET /internal/app/cameras/{id}/snapshot` | roster |
| `recordings(cam).list/url/frame_at` | `GET /internal/app/recordings/{id}[/url]`, `/internal/camera-agent/recordings/frame` | roster |
| `timeline.search/evidence` | `GET /internal/camera-agent/events[/{id}/evidence]` | roster |
| `timeline.plate_stats/summary/sessions` | `GET /internal/app/plates/*` | roster |
| `alerts.inbox()` | `GET /internal/app/alerts` | the app's own alerts |
| `state.get/set/delete/items` | `GET/PUT/DELETE /internal/app/state[/{key}]` | the app's own namespace |
| `ai.capabilities()` | KAI-C `GET /api/v1/ai/capabilities` | — |
| `ai.infer()` | KAI-C `POST /api/v1/infer/{adapter}` | — |
| `ai.stream()` → `InferStream` | KAI-C `WS /api/v1/infer/{adapter}/stream` (contract §6) | — |

Cameras are accepted as a `Camera`, an int id, or a `camN` / `cam-N`
handle everywhere. Reads return `None` / `[]` and log when the platform
is unreachable (an app must not die to a blip); writes raise
`PlatformError` so a lost state write is never silent.

## On an event loop: `opennvr_app_sdk.aio.AsyncOpenNVR`

The sync client is right for a detector loop. An app that serves a
`/ui` page, a FastAPI/Starlette service, or an agent loop that must
never block gets the same surface `await`-ed — same method names,
arguments, return types and degrade/raise rules, sharing the route
paths, request builders and parsers with the sync client (a parity test
in the SDK keeps the two identical):

```python
from opennvr_app_sdk.aio import AsyncOpenNVR

async with AsyncOpenNVR() as nvr:                 # or AsyncOpenNVR(http_client=pool)
    for cam in await nvr.cameras():
        jpeg = await nvr.snapshot(cam)
        result = await nvr.ai.infer("yolov8", jpeg, task="object_detection",
                                    camera_id=cam.handle)
    await nvr.state.set("last_seen", {"cam1": 12.5})
    recent = await nvr.timeline.search(camera="cam1", limit=5)
```

Pass `http_client=` to share one `httpx.AsyncClient` (a FastAPI
lifespan pool, a test transport); the client then leaves it open. The
one gap: `ai.stream()` (a blocking WebSocket session) has no async form
yet — call `ai.infer()` per frame from async code. The OpenNVR Agent's
capabilities probe is the first consumer.

## Durable state

`state` is a small key/value store in core, namespaced by app id: keys ≤
200 chars, JSON values ≤ 256 KB, ≤ 2000 keys per app — state, not
storage. It replaces the SQLite files and in-memory dicts apps invented
for cooldowns, registers and "last seen". `KeyedState` remains the right
tool for high-churn per-track TTL state.

## Consuming domain events

The read half of `docs/EVENT_CONTRACTS.md`:

```python
from opennvr_app_sdk import DomainEventSubscriber, domain_event_app

class Gate(DomainEventSubscriber):
    subscriptions = ["plate.recognized.v1", "access.decided.v1"]

    def on_event(self, event):          # DomainEvent: schema, camera_id, ts, payload
        ...

if __name__ == "__main__":
    raise SystemExit(domain_event_app(Gate, load_config=load_config).run())
```

Every message is decoded and checked against the envelope contract;
anything else is logged and skipped. The archetype runs the contract
server, self-registration and the config poll exactly like `Detector`
and `AlertSubscriber`.
