# Using the platform

One client, the app's own credential, everything core exposes to apps.

```python
from opennvr_app_sdk import OpenNVR

with OpenNVR() as nvr:          # OPENNVR_URL + the app's key, from the env
    for camera in nvr.cameras():          # only cameras assigned to this app
        jpeg = nvr.snapshot(camera)
```

| You want | Use |
|---|---|
| The camera roster this app was given | `nvr.cameras()`, `nvr.camera(id)` |
| A frame right now | `nvr.snapshot(camera)` |
| Clips, and a playable URL | `nvr.recordings(camera).list(...)`, `.url(...)`, `.frame_at(...)` |
| What was seen, and the proof | `nvr.timeline.search(...)`, `.evidence(id)` |
| Whether anyone acknowledged your alerts | `nvr.alerts.inbox(unacked=True)` |
| State that survives a restart | `nvr.state.get/set/delete/items` |
| To run a model | `nvr.ai.infer(adapter, jpeg, task=...)`, `nvr.ai.stream(...)` |

Every non-2xx raises `PlatformError`, so there is one exception type to
catch.

## Async

`AsyncOpenNVR` is the same surface, awaited. Use it inside an
archetype's run loop — a slow snapshot on one camera should not block
the others:

```python
--8<-- "cookbook/07_async_client.py:17:28"
```

## Fast inference

`KaiCClient.infer` is one HTTP round-trip per frame. At ten frames a
second use `InferStream` instead: the session stays open, the model
stays warm, and every frame shares one audit `correlation_id` — which is
what makes a sequence traceable as one episode.

## The past

`EventsClient` queries the platform's memory — what was seen, when, with
the evidence photo that proves it — so an app can answer "when did that
van last come?" without keeping an index of its own.

## Free answers: Tier-0

The platform runs a lightweight detector on every camera all the time.
Consuming it costs nothing: no adapter, no GPU, no poll. For "how many
people are at the loading dock?" it is the whole answer, and it already
knows which track has a good crop for evidence.

Full examples:
[`06_platform_client.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/06_platform_client.py),
[`13_events_client.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/13_events_client.py),
[`14_infer_stream.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/14_infer_stream.py),
[`11_tier0.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/11_tier0.py).
