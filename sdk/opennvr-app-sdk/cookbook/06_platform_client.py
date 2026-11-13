# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`OpenNVR` — everything an app reads from the platform.

Demonstrates: `OpenNVR`, `Camera`, `Camera.has_skill`, `Recording`,
`PlatformError`, `.cameras`, `.camera`, `.snapshot`, `.recordings`,
`.timeline`, `.alerts`, `.state`, `.ai`, `AppCredentials`,
`auth_headers`, `discover_cameras`, `cameras_for_skill`,
`full_frame_polygon`.

One client, the app's own credential, everything core exposes to apps.
The roster is already scoped: `nvr.cameras()` returns only the cameras
an operator assigned to this app, so an app cannot see the whole site
by accident.
"""
from opennvr_app_sdk import Camera, OpenNVR, PlatformError


def survey(nvr: OpenNVR) -> dict:
    """Read the roster and a frame from each camera."""
    report = {}
    for camera in nvr.cameras():                    # -> list[Camera]
        jpeg = nvr.snapshot(camera)                 # -> bytes | None
        report[camera.handle] = {
            "name": camera.name,
            "assigned_lpr": camera.has_skill("license-plate-recognition"),
            "snapshot_bytes": len(jpeg or b""),
        }
    return report


def recent_clips(nvr: OpenNVR, camera: Camera, start: str, end: str) -> list[str]:
    """Recordings and a playable URL for a window."""
    api = nvr.recordings(camera)
    clips = api.list(start=start, end=end)          # -> list[Recording]
    return [c.filename for c in clips]


def search_and_evidence(nvr: OpenNVR, camera: Camera) -> bytes | None:
    """The timeline is the platform's event index — what was seen, when,
    with the evidence frame that proves it."""
    hits = nvr.timeline.search(camera=camera, label="person", limit=10)
    if not hits:
        return None
    return nvr.timeline.evidence(hits[0]["id"])     # -> JPEG bytes


def durable_counters(nvr: OpenNVR) -> int:
    """Per-app key/value state that survives a restart — use it instead
    of a file, so the app stays stateless and container-friendly."""
    seen = int(nvr.state.get("plates_seen", 0))
    nvr.state.set("plates_seen", seen + 1)
    return seen + 1


def run_a_model(nvr: OpenNVR, camera: Camera) -> dict:
    """Inference without knowing where KAI-C lives or what the key is."""
    caps = nvr.ai.capabilities()                    # which adapters/tasks exist
    if not caps:
        return {}
    jpeg = nvr.snapshot(camera)
    if not jpeg:
        return {}
    return nvr.ai.infer("yolov8", jpeg, task="object_detection",
                        camera_id=camera.handle)


def who_raised_what(nvr: OpenNVR) -> list[dict]:
    """The alert inbox — what this app raised, and whether an operator
    has acknowledged it. Useful for a `/state` view of your own."""
    return nvr.alerts.inbox(unacked=True, limit=20)


def main() -> None:
    # No arguments: OPENNVR_URL and the app's own key come from the
    # environment the installer sets. Pass url=/token= to override.
    with OpenNVR() as nvr:
        try:
            print(survey(nvr))
        except PlatformError as exc:
            # Every non-2xx from core raises this — one exception type
            # to catch, with the status and body on it.
            print(f"platform said no: {exc}")


if __name__ == "__main__":
    main()
