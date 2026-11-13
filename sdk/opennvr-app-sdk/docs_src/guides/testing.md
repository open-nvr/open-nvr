# Testing

No broker, no core, no Docker. The forty lines every app's test suite
used to carry live in `opennvr_app_sdk.testing`, versioned with the SDK
so the shapes stay right when the contracts move.

```python
--8<-- "cookbook/16_testing.py:28:46"
```

## The three tests worth writing

1. **It fires on the thing.** One event, one alert, and the envelope
   carries the camera and correlation id a downstream consumer needs.
2. **It stays quiet on the near-miss.** The unwatched label, the
   confidence just below the floor, the object just outside the zone.
   This is the test that catches a rule which alerts on everything.
3. **The state machine re-arms.** For anything with `dwell` or
   `cooldown`: it fires once, stops, and fires again after the object
   leaves and returns.

## The builders

| Helper | Builds |
|---|---|
| `inference_event(*detections, camera_id=, completed_at=)` | an adapter `InferenceCompletedEvent` |
| `detection(label, confidence=, x=, y=, w=, h=, track_id=)` | one contract-shaped detection |
| `tier0_event(*tracks)` / `tier0_track(...)` | a Tier-0 event (pixel boxes, a frame size) |
| `domain_event(schema, payload, camera_id=)` | an EVENT_CONTRACTS.md envelope |
| `app_config(**keys)` | a config namespace — give it every key your rule reads |
| `RecorderChannel()` | an in-memory alert channel; `.dispatcher()`, `.alerts`, `.titles` |
| `feed(app, *events)` | drives events through the real decode path, returns what fired |
| `FakeCore(cameras=[...])` | a tiny in-process platform for apps that use `OpenNVR()` |

Pass `completed_at` explicitly whenever a rule measures duration — that
is how you test a dwell timer in microseconds instead of thirty seconds.

## Fixtures

```python
# conftest.py
pytest_plugins = ["opennvr_app_sdk.testing.pytest_plugin"]
```

gives you `recorder`, `app_config_factory` and `fake_core`.

## Before you ship

```bash
opennvr-app dev         # watch the rule against a simulated camera
opennvr-app validate .  # what a reviewer would check
opennvr-app spec        # the OpenAPI document your app will serve
```

Full example:
[`16_testing.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/16_testing.py).
