# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`App` — the facade, and the only file most apps need.

Demonstrates: `App`, `App.param`, `App.zone`, `App.metric`, `App.log`,
`@App.on_detection`, `@App.on_event`, `@App.on_setup`,
`@App.on_shutdown`, `@App.state`, `@App.action`, `DetectionEvent`,
`setting` / `"$name"`, `App.store`, `App.run`.

`App` compiles to a `Detector` (see `02_detector.py` for the class it
compiles to). Everything the decorators cannot express is still there
underneath — this is a shortcut, not a walled garden.

Run it:

    opennvr-app dev            # against a simulated camera
    python 01_app_facade.py --config config.yml
"""
from opennvr_app_sdk import App, Param

app = App(
    "driveway-watch",
    name="Driveway Watch",
    version="1.0.0",
    category="perimeter",
    summary="Alerts when someone loiters in the driveway, or a car is left there.",
    requires_tasks=["object_detection"],
    # Anything AppManifest accepts can be passed straight through.
    use_cases=["Alert on a person lingering by the cars at night"],
    author="OpenNVR",
    license="Apache-2.0",
)

# ── What the operator configures ───────────────────────────────────
#
# Each param is a manifest field, a config.yml key and an attribute on
# event.config — declared once. A rule filter written as "$name" reads
# the value at startup, so the operator can tune the rule without a
# code change.
app.param("night_only", bool, default=False,
          description="Only alert between sunset and sunrise.")
app.param("dwell_s", float, default=30.0,
          description="Seconds in the driveway before it counts as loitering.")

# A zone is drawn per camera by the operator, in the App Catalog. Naming
# it here is what puts it in the config form — and what tells them which
# polygon this app expects.
app.zone("driveway", "The gravel in front of the garage.")

# A dashboard, declared. The catalog renders these over app.store and
# @app.state below; there is no frontend to write.
app.metric("alerted", label="Alerts fired")
app.gauge("cars_parked", label="Cars in the driveway", min=0, max=6, warn=3)
app.log("recent", label="Recent activity", limit=20)


@app.on_setup()
def prepare(config) -> None:
    """Runs once with the parsed config, before any event."""
    app.store.update(alerted=0, cars_parked=0, recent=[])


# ── The rules ──────────────────────────────────────────────────────


@app.on_detection("person", zone="driveway", dwell="$dwell_s",
                  severity="high", emits="loitering")
def loitering(event) -> None:
    """A person, inside the driveway, for the configured dwell — fired
    once per presence episode, not once per frame.

    ``dwell`` measures time spent satisfying THIS rule's filters, so
    this is thirty seconds *in the driveway*, not thirty seconds on
    camera followed by one frame in it."""
    if event.config.night_only and not _is_night(event.ts):
        return
    event.alert(
        f"Person loitering on {event.camera}",
        f"Someone has been in the driveway for {event.dwell_s:.0f} seconds.",
        evidence={"other_objects": event.count("car")},
    )
    _note(event, "loitering")


@app.on_detection("car", "truck", zone="driveway", dwell=600, cooldown=3600,
                  severity="medium", emits="vehicle-left")
def vehicle_left(event) -> None:
    """A second rule on the same stream, with its own presence clock.
    ``cooldown`` stops a car that is simply parked from alerting every
    ten minutes."""
    event.alert(f"Vehicle left on {event.camera}")
    _note(event, "vehicle-left")


@app.on_event()
def crowding(camera_id, detections, event):
    """The escape hatch: the whole frame, for rules about the scene
    rather than one object. Returning alerts works here too."""
    people = [d for d in detections if d.get("label") == "person"]
    app.store["cars_parked"] = sum(
        1 for d in detections if d.get("label") in ("car", "truck"))
    if len(people) >= 5:
        from opennvr_app_sdk import Alert
        return [Alert(title=f"{len(people)} people on {camera_id}",
                      description="Crowding in view.", camera_id=camera_id,
                      severity="medium", tags=["crowding"])]
    return []


# ── The app's own surfaces ─────────────────────────────────────────


@app.state()
def live_state() -> dict:
    """Anything in ``app.store`` is included automatically; return extra
    keys here when they need computing."""
    return {"zone_configured": bool(app.config.driveway)}


@app.action("mute", label="Mute for an hour", confirm=True,
            params=[Param("minutes", int, default=60)])
def mute(minutes: int = 60) -> dict:
    """An operator button in the catalog, with a generated form."""
    app.store["muted_until"] = minutes
    return {"muted_for_minutes": minutes}


@app.on_shutdown()
def cleanup() -> None:
    """SIGINT / SIGTERM, after the loop stops."""
    app.store.clear()


# ── Helpers ────────────────────────────────────────────────────────


def _note(event, kind: str) -> None:
    app.store["alerted"] += 1
    app.store["recent"] = ([f"{kind} on {event.camera}"] + app.store["recent"])[:20]


def _is_night(ts: float) -> bool:
    import datetime as dt
    hour = dt.datetime.fromtimestamp(ts, dt.timezone.utc).hour
    return hour >= 19 or hour < 6


if __name__ == "__main__":
    raise SystemExit(app.run())
