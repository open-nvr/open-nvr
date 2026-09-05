# opennvr-app-sdk

The SDK for building vision apps on [OpenNVR](https://opennvr.org), the
self-hosted AI video platform. Cameras, Tier-0 detections, pluggable
inference, an event bus, recordings, an operator alert inbox, users and
per-camera permissions, and a catalog every deployment opens — all
already running. You write the rule, the model or the workflow; this
package is the only import you need.

Apache-2.0 — ship your app under any licence, closed included.

```bash
pip install opennvr-app-sdk
```

## A detector in one method

```python
from opennvr_app_sdk import Alert, AppManifest, Detector, Param, app

class Loitering(Detector):
    manifest = AppManifest(
        id="loitering", name="Loitering", version="1.0.0", category="perimeter",
        requires_tasks=["object_detection"],
        params=[Param("dwell_s", float, default=30.0)],
    )

    def setup(self):
        self.present = self.keyed_state(ttl=10.0)   # forgets a camera 10 s after the last person

    def on_detections(self, camera_id, detections, event):
        people = [d for d in detections if d.get("label") == "person"]
        if not people:
            return
        rec = self.present.touch(camera_id)
        if rec.age >= self.cfg.dwell_s and not rec.alerted:
            rec.alerted = True
            yield Alert(title="Loitering", description=f"{len(people)} person(s) for {rec.age:.0f}s",
                        camera_id=camera_id, severity="medium")

if __name__ == "__main__":
    raise SystemExit(app(Loitering).run())
```

That app subscribes to the platform's detections, serves the registry
contract (`/health`, `/state`, `/manifest`, config form, actions),
registers itself in the App Catalog, is issued its own credential, and
fires alerts that reach the operator inbox — none of which you wrote.

## The platform, from an app

```python
from opennvr_app_sdk import OpenNVR            # or opennvr_app_sdk.aio.AsyncOpenNVR

nvr = OpenNVR()                                 # OPENNVR_URL + the app's key
for cam in nvr.cameras():                       # only cameras assigned to this app
    jpeg = nvr.snapshot(cam)
    det = nvr.ai.infer("yolov8", jpeg, task="object_detection", camera_id=cam.handle)
nvr.state.set("last_seen", {"cam1": 12.5})      # durable, survives restarts
clips = nvr.recordings("cam1").list(start="2026-09-05T00:00:00Z")
mine = nvr.alerts.inbox(unacked=True)
```

## Where to go next

* [Developer program](https://github.com/open-nvr/open-nvr/blob/main/docs/DEVELOPER_PROGRAM.md) — the deal: no fee, your licence, the compatibility promise.
* [Your first detector](https://github.com/open-nvr/open-nvr/blob/main/docs/FIRST_DETECTOR.md) — scaffold, run against a stack, list in the catalog.
* [SDK reference](https://github.com/open-nvr/open-nvr/blob/main/docs/SDK_REFERENCE.md) — every public name, by task.
* [Platform client](https://github.com/open-nvr/open-nvr/blob/main/docs/APP_PLATFORM.md), [app surfaces](https://github.com/open-nvr/open-nvr/blob/main/docs/APP_SURFACES.md), [credentials](https://github.com/open-nvr/open-nvr/blob/main/docs/APP_CREDENTIALS.md), [event contracts](https://github.com/open-nvr/open-nvr/blob/main/docs/EVENT_CONTRACTS.md).
* [Example apps](https://github.com/open-nvr/open-nvr/tree/main/examples) — thirteen first-party apps built on this SDK.

Compatibility: the app-registry contract is versioned (`api_version`)
and tested in the OpenNVR repository on every change; `min_sdk_version`
only moves when an older SDK would misbehave. Issues tagged `sdk` at
[open-nvr/open-nvr](https://github.com/open-nvr/open-nvr/issues).
