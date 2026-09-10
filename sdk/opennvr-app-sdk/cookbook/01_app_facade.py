# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`App` — the facade, and the only file most apps need.

Demonstrates: `App`, `App.param`, `App.emits`, `@App.on_detection`,
`@App.on_event`, `@App.on_setup`, `DetectionEvent`, `App.run`.

`App` compiles to a `Detector` (see `02_detector.py` for the class it
compiles to). Everything the decorators cannot express is still there
underneath — this is a shortcut, not a walled garden.

Run it:

    opennvr-app dev            # against a simulated camera
    python 01_app_facade.py --config config.yml
"""
from opennvr_app_sdk import App

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

# Operator-settable knobs: a manifest param, a config.yml field, and an
# attribute on `event.config` — declared once.
app.param("night_only", bool, default=False,
          description="Only alert between sunset and sunrise.")
app.param("dwell_s", float, default=30.0,
          description="Seconds before a person counts as loitering.")

# Declared alert kinds, so the catalog can document and route them.
# Omit this and one is derived per rule from the function name.
app.emits("loitering", severity="high", description="A person stayed too long.")
app.emits("vehicle-left", severity="medium", description="A car parked and stayed.")


@app.on_setup()
def prepare(cfg) -> None:
    """Runs once with the parsed config, before any event."""
    app.plates_seen: set[str] = set()          # any state you like


@app.on_detection("person", zone="driveway", dwell=30, severity="high")
def loitering(event) -> None:
    """The rule: a person, inside the driveway zone, for 30 seconds —
    fired once per presence episode, not once per frame."""
    if event.config.night_only and not _is_night(event.ts):
        return
    event.alert(
        f"Person loitering on {event.camera}",
        f"Someone has been in the driveway for {event.dwell_s:.0f} seconds.",
        evidence={"other_objects": event.count("car")},
    )


@app.on_detection("car", "truck", zone="driveway", dwell=600, cooldown=3600)
def vehicle_left(event) -> None:
    """A second rule on the same stream. `cooldown` stops a car that is
    simply parked from alerting every ten minutes."""
    event.alert(f"Vehicle left on {event.camera}", severity="medium")


@app.on_event()
def crowding(camera_id, detections, event):
    """The escape hatch: the whole frame, for rules about the scene
    rather than one object. Returning alerts works here too."""
    people = [d for d in detections if d.get("label") == "person"]
    if len(people) >= 5:
        from opennvr_app_sdk import Alert
        return [Alert(title=f"{len(people)} people on {camera_id}",
                      description="Crowding in view.", camera_id=camera_id,
                      severity="medium", tags=["crowding"])]
    return []


def _is_night(ts: float) -> bool:
    import datetime as dt
    hour = dt.datetime.fromtimestamp(ts, dt.timezone.utc).hour
    return hour >= 19 or hour < 6


if __name__ == "__main__":
    raise SystemExit(app.run())
