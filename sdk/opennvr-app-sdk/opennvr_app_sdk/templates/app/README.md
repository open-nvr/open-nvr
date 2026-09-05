# __APP_NAME__

An OpenNVR **Detector** app: it subscribes to the platform's inference
stream (`opennvr.inference.>`) and fires alerts on the detections it
cares about — zero adapter GPU cost, it rides inference another app is
already driving.

Scaffolded by `opennvr-app new`. The walkthrough is
[FIRST_DETECTOR.md](__DOCS__FIRST_DETECTOR.md).

## Quick start

```bash
uv sync                       # __SYNC_HINT__
uv run pytest -q              # the smoke test — should be GREEN
cp config.example.yml config.yml
# edit config.yml: nats_url, nats_token, watch_labels
uv run python __APP_MODULE__.py --config config.yml
```

`--once` processes a single event then exits; `--log-level DEBUG` is
verbose; `SIGINT` / `SIGTERM` drain and exit cleanly.

## Where the rule lives

Everything except **the rule** is the SDK's. The rule is one method —
`on_detections` in [`__APP_MODULE__.py`](__APP_MODULE__.py). The starter
fires on any sighting of a watched label; replace the body with your
predicate — a zone (`opennvr_app_sdk.Zone`), a dwell timer
(`self.keyed_state(ttl=...)`), a confidence gate, a time window.

When the rule needs more than the event in hand — the cameras assigned
to this app, a snapshot, past events, state that survives a restart —
use the platform client rather than talking to core yourself:

```python
from opennvr_app_sdk import OpenNVR
nvr = OpenNVR()                       # OPENNVR_URL + the app's own key
nvr.cameras(); nvr.snapshot(camera_id); nvr.state.get("last_seen")
```

Reference: [APP_PLATFORM.md](__DOCS__APP_PLATFORM.md),
[SDK_REFERENCE.md](__DOCS__SDK_REFERENCE.md),
[APP_SURFACES.md](__DOCS__APP_SURFACES.md) (config form, state views,
actions, selling your app).

## Layout

```
__APP_ID__/
├── __APP_MODULE__.py     Manifest + AppConfig + the rule + CLI
├── config.example.yml    What an operator configures
├── pyproject.toml        __PYPROJECT_HINT__
├── Dockerfile            __DOCKERFILE_HINT__
└── tests/test_smoke.py   The parity bar — on_detections fires an alert
```

## List it in the App Catalog

Every OpenNVR install browses the curated index. Add one entry —
installable from your image, or `kind: external` linking to where you
distribute it — per [CONTRIBUTING_APPS.md](__DOCS__CONTRIBUTING_APPS.md);
the deal for developers is [DEVELOPER_PROGRAM.md](__DOCS__DEVELOPER_PROGRAM.md).
