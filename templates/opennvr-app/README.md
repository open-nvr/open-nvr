# __APP_NAME__

An OpenNVR **Detector** app: it subscribes to KAI-C's NATS inference
stream (`opennvr.inference.>`) and fires alerts on the detections it
cares about. Zero adapter GPU cost — it rides inference another app is
already driving.

Scaffolded from `templates/opennvr-app`. The full walkthrough is
[`docs/FIRST_DETECTOR.md`](../../docs/FIRST_DETECTOR.md) — "Your first
OpenNVR detector in 15 minutes".

## Quick start

```bash
uv sync                       # installs the SDK (editable) + pytest
uv run pytest -q              # the smoke test — should be GREEN
cp config.example.yml config.yml
# edit config.yml: nats_url, nats_token, watch_labels
uv run python __APP_MODULE__.py --config config.yml
```

`--once` processes a single event then exits (smoke-test the live
wiring); `--log-level DEBUG` is verbose. `SIGINT` / `SIGTERM` drains the
NATS connection and exits cleanly.

## Where the rule lives

Everything except **the rule** is boilerplate the SDK owns. The rule is
one method — `on_detections` in
[`__APP_MODULE__.py`](__APP_MODULE__.py):

```python
def on_detections(self, camera_id, detections, event) -> list[Alert]:
    matches = [d for d in detections
               if str(d.get("label", "")).lower() in self.cfg.watch_labels]
    if not matches:
        return []
    label = str(matches[0].get("label", "")).lower()
    return [self._build_alert(camera_id=camera_id, label=label, event=event)]
```

The starter fires on any sighting of a watched label. Replace the body
with your predicate — a zone check (`opennvr_app_sdk.geometry.Zone`), a
dwell timer (`opennvr_app_sdk.state.keyed_state`), a confidence gate, a
time-of-day window. The [`loitering-detection`](../../examples/loitering-detection)
example is a full worked state machine to copy from.

When the rule needs more than the event in hand — the cameras assigned
to this app, a snapshot, past events, state that survives a restart —
use the platform client instead of talking to core yourself:

```python
from opennvr_app_sdk import OpenNVR
nvr = OpenNVR()                       # OPENNVR_URL + the app's own key
nvr.cameras(); nvr.snapshot(camera_id); nvr.state.get("last_seen")
```

See [`docs/APP_PLATFORM.md`](../../docs/APP_PLATFORM.md) and the index in
[`docs/SDK_REFERENCE.md`](../../docs/SDK_REFERENCE.md).

## Layout

```
__APP_ID__/
├── __APP_MODULE__.py     Manifest + config parse + the rule (on_detections) + CLI
├── config.example.yml    What an operator configures
├── pyproject.toml        Editable SDK dep + dev group
├── Dockerfile            SDK-install image (build from the repo root)
├── README.md             you are here
└── tests/
    └── test_smoke.py     The parity bar — on_detections fires an alert
```

## Publish to the App Store

An operator browses and installs apps from the **App Catalog** (Settings
→ App Catalog). Getting yours there is a reviewed, validated PR that adds
one entry to the curated index. Full flow — build, publish + digest-pin
your image, add the index entry, `make validate-apps-index` — in
[`docs/CONTRIBUTING_APPS.md`](../../docs/CONTRIBUTING_APPS.md).
