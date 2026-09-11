# OpenNVR App SDK

Build a vision app on [OpenNVR](https://opennvr.org), the self-hosted AI
video platform. Cameras, always-on detection, pluggable inference, an
event bus, recordings, an operator alert inbox, users with per-camera
permissions, and a catalog every deployment opens — all already running.
You write the rule, the model or the workflow; this package is the only
import you need.

```bash
pip install opennvr-app-sdk
```

**Apache-2.0.** Ship your app under any licence, closed included, with
no fee to OpenNVR — see [Licensing](licensing.md) for why that holds
even though the platform core is AGPL.

## A whole app

```python
from opennvr_app_sdk import App

app = App("driveway-watch", name="Driveway Watch", category="perimeter")
app.param("dwell_s", float, default=30.0)

@app.on_detection("person", zone="driveway", dwell="$dwell_s")
def loitering(event):
    event.alert(f"Person loitering on {event.camera}", severity="high")

if __name__ == "__main__":
    raise SystemExit(app.run())
```

That app subscribes to the platform's detections, serves the registry
contract (`/health`, `/manifest`, `/state`, `/openapi.json`, config
form, actions), registers itself in the App Catalog, is issued its own
credential, and fires alerts that reach the operator inbox — none of
which you wrote. The zone becomes an editor the operator draws on a
camera still; the dwell timer, the once-per-episode latch, the alert
envelope and the config file are the SDK's.

## Where to go next

<div class="grid cards" markdown>

- **[Quickstart](quickstart.md)** — a running app in about ten minutes,
  with no broker and no Docker.
- **[Concepts](concepts.md)** — the four archetypes, and how to pick one.
- **[Cookbook](cookbook.md)** — one runnable example per class.
- **[API reference](reference/front-door.md)** — every export, in tiers.
- **[Specs](specs.md)** — the OpenAPI and AsyncAPI your app publishes.

</div>

## The deal for developers

OpenNVR takes **no fee** on apps. Your app is yours, under your licence,
at your price; the platform gives you the licence hook and stays out of
the transaction. See [Selling an app](guides/selling.md) and the
[developer program](https://github.com/open-nvr/open-nvr/blob/main/docs/DEVELOPER_PROGRAM.md).
