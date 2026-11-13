# __APP_NAME__

An OpenNVR app: it subscribes to the platform's inference stream
(`opennvr.inference.>`) and fires alerts on the detections it cares
about — zero adapter GPU cost, it rides inference another app is
already driving.

Scaffolded by `opennvr-app new`. The walkthrough is
[FIRST_DETECTOR.md](__DOCS__FIRST_DETECTOR.md).

## Quick start

```bash
uv sync                       # __SYNC_HINT__
uv run pytest -q              # the smoke test — should be GREEN
uv run opennvr-app dev        # watch the rule fire against a simulated camera
cp config.example.yml config.yml
# edit config.yml: nats_url, nats_token, min_confidence
uv run python __APP_MODULE__.py --config config.yml
```

`opennvr-app dev` needs no broker, no adapter and no container: it
walks a simulated object across the frame and prints every alert your
rule fires. `--still` parks it in the centre, `--label car` changes
what it sees, `--fast` skips the wait between events.

`--once` processes a single event then exits; `--log-level DEBUG` is
verbose; `SIGINT` / `SIGTERM` drain and exit cleanly.

## Where the rule lives

Everything except **the rule** is the SDK's. The rule is one decorated
function in [`__APP_MODULE__.py`](__APP_MODULE__.py):

```python
@app.on_detection("person", zone="driveway", dwell=30, cooldown=60)
def loitering(event):
    event.alert(f"Person loitering on {event.camera}", severity="high")
```

The filters do the work most rules used to hand-roll: `zone=` adds the
per-camera zone editor to the catalog and only fires inside it,
`dwell=` waits for continuous presence and fires once per episode,
`cooldown=` throttles a repeat. The `event` carries `camera`, `label`,
`confidence`, `track_id`, `zone`, `dwell_s`, `count("car")`, `config`,
and `alert()` — which fills in the whole §11.5 envelope from what it
already knows.

`@app.on_event()` hands you the raw `(camera_id, detections, event)`
triple for rules about the frame rather than one object;
`@app.on_setup()` runs once with the parsed config. When a rule outgrows
all of that, subclass `Detector` directly — `App` compiles to one.

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
├── __APP_MODULE__.py     The app: identity, params, the rule
├── config.example.yml    What an operator configures
├── pyproject.toml        __PYPROJECT_HINT__
├── Dockerfile            __DOCKERFILE_HINT__
└── tests/test_smoke.py   The parity bar — the rule fires an alert
```

## List it in the App Catalog

Every OpenNVR install browses the curated index. Add one entry —
installable from your image, or `kind: external` linking to where you
distribute it — per [CONTRIBUTING_APPS.md](__DOCS__CONTRIBUTING_APPS.md);
the deal for developers is [DEVELOPER_PROGRAM.md](__DOCS__DEVELOPER_PROGRAM.md).
__REPO_SECTION__