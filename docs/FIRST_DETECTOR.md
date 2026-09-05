# Your first OpenNVR detector in 15 minutes

This is the on-ramp. By the end you'll have a **real, runnable OpenNVR
app** — generated from a template, tested green against the actual SDK,
running against the stack, and one PR away from the App Store. No
copy-pasting a whole example and deleting what you don't need: a
generator scaffolds a minimal working app, and you fill in **one
method** — the rule.

> **Prerequisites:** [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.
> No Docker needed for steps 1–3. A running OpenNVR stack (step 4) is
> only needed to see it fire against live cameras.

The SDK ships three archetypes. This guide builds a **Detector**: it
*subscribes* to KAI-C's NATS inference stream and reacts to detections
another app is already driving — so it pays **zero adapter GPU cost**.
(`FrameApp` *drives* inference itself — see
[`examples/intrusion-detection`](../examples/intrusion-detection) — and
`AlertSubscriber` is the pass-through archetype that rides the alert bus,
like [`examples/home-assistant-relay`](../examples/home-assistant-relay).)

---

## 1. Generate the app (1 min)

From the repo root:

```bash
python3 scripts/create_opennvr_app.py package-watch --task object_detection
```

`package-watch` is your app id (kebab-case; it becomes `AppManifest.id`
and the folder name). `--task` names the adapter task your app rides —
`object_detection` here (see the vocabulary in
[`docs/AI_ADAPTER_CONTRACT.md` §4](AI_ADAPTER_CONTRACT.md)). The
generator drops the app in `examples/package-watch/` by default (pass
`--dest` to put it elsewhere) and prints your next steps.

It substitutes every placeholder — id, snake-case module name,
PascalCase class, Title-cased human name, task — so what lands is a
compiling app, not a template with holes:

```
examples/package-watch/
├── package_watch.py       Manifest + config parse + THE RULE + CLI
├── config.example.yml     What an operator configures
├── pyproject.toml         Editable SDK dep + dev group
├── Dockerfile             SDK-install image
├── README.md
└── tests/
    └── test_smoke.py      The parity bar
```

## 2. Look at the rule (3 min)

Everything except **the rule** is boilerplate the SDK's `Detector` base
owns: the NATS subscribe/decode loop, per-message exception isolation,
the `camera_id` + `result.detections` payload walk, `completed_at`
timestamp parsing, alert dispatch, the CLI, and signal handling. What's
left for you is one method, `on_detections`, in `package_watch.py`.
Here's the heart of what the generator wrote (abridged — the file on
disk carries fuller docstrings and type annotations):

```python
def on_detections(self, camera_id, detections, event) -> list[Alert]:
    """THE RULE — this is the one method you fill in.

    Called once per decoded inference event that has a camera_id and a
    result.detections list. Return the Alert objects to fire (the SDK
    base dispatches them). Return [] to stay quiet.
    """
    # Each detection is a §5.1 dict: {"label", "confidence", "bbox":
    # {"x","y","w","h"}, "track_id", "attributes"}. bbox coords are
    # normalized (0..1). Filter to the labels this app watches.
    matches = [
        det
        for det in detections
        if isinstance(det, dict)
        and str(det.get("label", "")).lower() in self.cfg.watch_labels
    ]
    if not matches:
        return []

    label = str(matches[0].get("label", "")).lower()
    # TODO: put YOUR rule here. This starter alerts on any sighting.
    return [self._build_alert(camera_id=camera_id, label=label, event=event)]
```

The starter fires on **any** sighting of a watched label. That's your
canvas. **Where you put your rule** is the `# TODO` line — replace the
trivial "any match" with your predicate:

| You want… | Reach for | Example to copy |
|---|---|---|
| Fire only inside a drawn zone | `opennvr_app_sdk.geometry.Zone` + `bbox_center` | [`loitering-detection`](../examples/loitering-detection) |
| Fire after a dwell threshold | `opennvr_app_sdk.state.keyed_state` (TTL + latch) | [`loitering-detection`](../examples/loitering-detection) |
| Count in a zone, alert on a band | edge-triggered counter | [`occupancy-counting`](../examples/occupancy-counting) |
| Track the same object across frames | key state on `det["track_id"]` | [`line-crossing`](../examples/line-crossing) |
| Gate on confidence / time-of-day | plain Python in the filter | — |

Add config knobs as `Param(...)` entries in the `MANIFEST` and fields on
`AppConfig` — the catalog renders every param as a form field, so an
operator tunes your rule without touching code.

## 3. Run the tests green (2 min)

The generated `tests/test_smoke.py` is the **parity bar**: it constructs
your detector and drives it through the SDK's real decode → rule →
dispatch path (an in-memory recorder captures alerts — no broker
needed), asserting a matching detection fires exactly one alert.

```bash
cd examples/package-watch
uv sync                 # installs the SDK (editable) + pytest
uv run pytest -q
```

```
.....                                                    [100%]
5 passed in 0.25s
```

Green means your app is wired to the SDK correctly *before* you've
written a line of your own logic. As you replace the starter rule, keep
this file green — extend it to pin your predicate (a below-threshold
case stays quiet, an in-zone case fires, etc.), exactly the way the
shipped examples' tests do.

## 4. Run it against the stack (5 min)

```bash
cp config.example.yml config.yml
# edit config.yml: nats_url, nats_token (the stack's INTERNAL_API_KEY),
# and watch_labels for your scene
uv run python package_watch.py --config config.yml --once   # one event then exit
```

`--once` processes a single inference event and exits — the fastest way
to confirm live wiring. Drop it to run as a daemon; `SIGINT` / `SIGTERM`
drains cleanly.

To run it **inside** the stack alongside the other detector apps, build
its image and add it to the apps overlay
([`docker-compose.apps.yml`](../docker-compose.apps.yml)) the same way
`loitering-detection` is wired — the app's `Dockerfile` builds from the
repo root (the SDK is baked in), and on boot it subscribes to NATS,
serves the SDK contract endpoints (`/health` `/manifest` `/state`), and
self-registers with the app registry so it shows up in **Settings → App
Catalog**:

```bash
# from the repo root, on top of the standard stack:
docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile apps up -d
```

Uncomment the `contract_port` / `opennvr_url` block in `config.yml` (the
generator ships it commented) to turn the contract surface + catalog
self-registration on.

On first registration core issues the app **its own key** (`oak_…`),
which the SDK stores and uses from then on — the site key in
`config.yml` is only for bootstrap ([APP_CREDENTIALS.md](APP_CREDENTIALS.md)).
With that key, `OpenNVR()` gives the rule everything else it might
want from the platform — the cameras assigned to it, a snapshot, past
events, a place to keep state across restarts — without touching NATS
or HTTP yourself:

```python
from opennvr_app_sdk import OpenNVR

nvr = OpenNVR()                       # OPENNVR_URL + the app's key
for cam in nvr.cameras():             # only cameras assigned to this app
    last = nvr.state.get(f"seen:{cam.handle}")
    jpeg = nvr.snapshot(cam)
```

The full client is in [APP_PLATFORM.md](APP_PLATFORM.md); inside `/ui`
and actions, `current_user()` tells you who is asking and which cameras
they may see ([APP_SURFACES.md §5](APP_SURFACES.md)).

## 5. Publish to the App Store (2 min to start)

An operator installs apps from the **App Catalog**. Getting yours there
is a reviewed, validated PR that appends **one entry** to the curated
index — mirroring the `AppManifest` you already wrote (id, name,
version, category, `requires_tasks`, `emits`). Build + publish +
digest-pin your image, add the entry, run `make validate-apps-index`,
open the PR. The full walkthrough and the trust model behind the curated
index are in **[`docs/CONTRIBUTING_APPS.md`](CONTRIBUTING_APPS.md)**.

---

## Why this matters: two doors, one app

An OpenNVR app you build on the SDK is load-bearing **two ways at once**:

1. **A catalog card.** Its `AppManifest` renders a browse-and-install
   card and an auto-generated config form in the App Catalog — a
   deploy-and-forget monitoring app. This is the door you just walked
   through.
2. **A conversational skill.** Because the app declares its `requires_tasks`
   and rides the same NATS surface, it becomes a capability the
   [`camera-agent`](../examples/camera-agent) can discover and invoke —
   "is anyone loitering by the shed?" resolves through the same task
   index that greys your catalog card. The declarative manifest is what
   makes an app *both* a thing you install *and* a thing you can ask.

That's the platform bet: you write **one rule** on **one SDK**, and it
shows up as a catalog card an operator installs *and* as a skill the
agent can reach for. Write the predicate once; both doors open. The full
story — including the capability-matching mechanism that decides when
either door can actually open — is [`docs/TWO_DOORS.md`](TWO_DOORS.md).

---

## Related reading

- [`docs/DEVELOPER_PROGRAM.md`](DEVELOPER_PROGRAM.md) — the deal: what
  the platform gives you, selling apps and models, the compatibility
  promise.
- [`docs/SDK_REFERENCE.md`](SDK_REFERENCE.md) — every public SDK name,
  by task; [`docs/APP_PLATFORM.md`](APP_PLATFORM.md) — the `OpenNVR`
  client; [`docs/APP_CREDENTIALS.md`](APP_CREDENTIALS.md) — the app's
  own key.
- [`docs/APP_SURFACES.md`](APP_SURFACES.md) — **the next step after this
  guide**: make your app a full citizen — agent skill, live config,
  state views, and action forms, all declared, no frontend.
- [`docs/TWO_DOORS.md`](TWO_DOORS.md) — the two-doors model in full:
  catalog card + conversational skill from one `Detector`, and the task
  intersection behind both.
- [`examples/README.md`](../examples/README.md) — the gallery, the
  drives-vs-subscribes axis grid, and how an example folder is structured.
- [`docs/CONTRIBUTING_APPS.md`](CONTRIBUTING_APPS.md) — the App Store
  submission flow and the curated-index trust model.
- [`sdk/opennvr-app-sdk/`](../sdk/opennvr-app-sdk/) — the SDK: `Detector`,
  `AppManifest` / `Param` / `AlertType` / `StateView` / `Action`,
  `keyed_state`, `geometry`. Beyond params→config-form, a manifest can
  declare **state views** (the catalog renders your `GET /state` live —
  no UI code) and **actions** (operator verbs like footage-search's
  "Search footage" form, proxied user-JWT-only); with `opennvr_url` set,
  registry config edits are **delivered live** to your running app via
  `on_config_update` — see the LPR watchlists for the pattern.
- [`docs/AI_ADAPTER_CONTRACT.md`](AI_ADAPTER_CONTRACT.md) — the inference
  event shape your rule consumes and the `tasks_advertised` vocabulary
  behind `requires_tasks`.
