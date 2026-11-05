"""The app's identity for the App Catalog — the same ``AppManifest`` every
SDK app serves, declared ``ui_mode="external"`` like the Camera Agent: the
catalog shows an "Open app" button that opens the app's own web UI in a
new tab, and the dedicated URL is also carried on the card."""
from __future__ import annotations

from typing import Any

from . import APP_ID, APP_VERSION
from .config import Settings, get_settings


def app_ui_url(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    public = (s.public_url or "").rstrip("/")
    if public:
        return f"{public}/"
    return f"http://{{host}}:{s.port}/"


def app_manifest(settings: Settings | None = None) -> dict[str, Any]:
    from opennvr_app_sdk import AppManifest
    from opennvr_app_sdk.manifest import AlertType

    s = settings or get_settings()
    url = app_ui_url(s)
    return AppManifest(
        id=APP_ID,
        name="Attendly",
        version=APP_VERSION,
        category="attendance",
        summary=(
            "Classroom attendance from your cameras: enrol students by photo or "
            "webcam, plan rooms, courses and a weekly timetable, and let each "
            "class take recognition snapshots that finalise into a PRESENT/ABSENT "
            "roster. Faces are recognised by the InsightFace adapter through KAI-C."
        ),
        description=(
            "Attendly runs beside OpenNVR as its own application with its own web "
            f"UI at {url} (open it from this card, or bookmark the address). "
            "Sign in with your OpenNVR account. Rooms are OpenNVR cameras; frames "
            "and detections come from the platform."
        ),
        requires_tasks=[],
        requires_adapters=["insightface"],
        # Gate decisions go on the bus as access.decided.v1 (the
        # gate-controller app actuates): a producer by declaration.
        provides=["attendance"],
        emits=[
            AlertType("hostel_in", severity="low", description="A student entered the hostel (name and block)."),
            AlertType("hostel_out", severity="low", description="A student left the hostel (name and block)."),
            AlertType("gate_denied", severity="medium",
                      description="Someone a gate's policy does not admit: unknown face, day scholar at a "
                                  "hostel, another block, or no meal booking."),
            AlertType("mess_served", severity="low", description="A booked student entered the mess."),
        ],
        # A platform component on the site key, like the agent: no picker.
        camera_picker=False,
        all_cameras=False,
        has_ui=True,
        ui_mode="external",
        ui_url=url,
        website=url,
        author="OpenNVR",
        license="AGPL-3.0",
        use_cases=["classroom attendance", "lecture-hall roll call", "training-centre presence",
                   "hostel flap barriers", "mess entry against meal bookings", "staff attendance"],
    ).to_dict()
