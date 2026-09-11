# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Tier-0 — answer from the always-on detector, for free.

Demonstrates: `consume_tier0`, `is_tier0_subject`, `snapshot_from_event`,
`Tier0Snapshot`, `.counts`, `.count`, `.present`, `.describe`,
`.tracks_with_best`, `describe_counts`, `tier0_to_detections`,
`BestFrameClient`, `make_best_frame_fetch`.

Tier-0 is the lightweight detector the platform runs on every camera all
the time. Consuming it costs nothing extra — no adapter, no GPU, no
poll — and it already knows what is in frame right now and which track
has a good crop. For "how many people are at the loading dock?" it is
the whole answer.

Two ways in: let the Detector base bridge Tier-0 into ordinary
detections (`consume_tier0 = True`), or read the snapshot directly when
you want counts and best frames rather than boxes.
"""
from typing import Any

from opennvr_app_sdk import (
    Alert, AppManifest, Detector, is_tier0_subject, make_best_frame_fetch,
    snapshot_from_event,
)

MANIFEST = AppManifest(
    id="dock-watch", name="Dock Watch", version="1.0.0", category="analytics",
    summary="Reports what is at the loading dock, from Tier-0 alone.",
    requires_tasks=[],                   # ← no adapter needed at all
    subscribes="opennvr.inference.tier0.>",
)


class DockWatch(Detector):
    manifest = MANIFEST

    #: Off by default on purpose: an app also subscribed to a heavy
    #: adapter would otherwise see the same object twice and alert
    #: twice. Turn it on when Tier-0 is your ONLY source.
    consume_tier0 = True

    def setup(self) -> None:
        self.latest: dict[str, Any] = {}
        # A coroutine that fetches the best crop for a camera's most
        # recent track — for evidence on an alert, or a gallery view.
        self.best_frame = make_best_frame_fetch("http://opennvr-core:8000")

    def on_detections(self, camera_id, detections, event) -> list[Alert]:
        """With `consume_tier0`, Tier-0 tracks arrive here as ordinary
        contract-shaped detections — the same rule works for both
        sources, which is the point."""
        people = [d for d in detections if d.get("label") == "person"]
        if len(people) < 3:
            return []
        return [Alert(title=f"{len(people)} people at {camera_id}",
                      description="Crowding at the dock.",
                      camera_id=camera_id, severity="medium")]

    def state_snapshot(self) -> dict[str, Any]:
        return {"latest": self.latest}


def read_the_snapshot(subject: str, payload: dict[str, Any]) -> str | None:
    """The direct route: counts and a speakable phrase, without going
    through detections at all. This is what the camera agent uses to
    answer 'what do you see?' with no inference call."""
    if not is_tier0_subject(subject):
        return None
    snapshot = snapshot_from_event(payload)      # -> Tier0Snapshot
    if not snapshot.total:
        return "nothing in view"
    if snapshot.present("person") and snapshot.count("car") > 1:
        # e.g. "a person, 2 cars"
        return f"{snapshot.describe()} at {snapshot.camera_id}"
    return snapshot.describe()


def evidence_candidates(payload: dict[str, Any]) -> list[Any]:
    """Tracks that advertise a fetchable best frame — the crop worth
    attaching to an alert, already chosen by the platform."""
    snapshot = snapshot_from_event(payload)
    return [t.get("id") for t in snapshot.tracks_with_best()]
