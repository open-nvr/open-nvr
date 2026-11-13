# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier-0 sees something, and it becomes a visit you can look at.

The longest chain in the product short of LPR: frames off RTSP, a motion gate,
YOLOv8, tracking, best-frame selection, a POST to core when the track ends, a
row in ``events``, and an evidence JPEG on disk. Every link fails silently. The
Vehicles and timeline pages simply stay empty, with nothing in any log saying
why, which is exactly what makes this worth an end-to-end test.

**These need real footage.** Tier-0 runs YOLOv8, which classifies COCO objects,
so a generated clip produces motion, no detection, no track and no visit. The
``detectable_camera`` fixture skips with that explanation when the rig has only
generated clips — a skip that says why beats a failure that blames the product.

The guards run first on purpose. Without them a wedged motion gate and a
genuinely broken detector are the same symptom: an empty event list two hundred
seconds later.
"""

from __future__ import annotations

import pytest

from harness import routes
from harness.budgets import BUDGETS
from harness.guards import require_tier0_calibrated
from harness.waiting import eventually

pytestmark = pytest.mark.detection


def _visits(client, camera_id: int) -> list[dict]:
    return client.json(
        routes.EVENTS, params={"camera_id": camera_id, "limit": 50}
    )["events"]


def test_footage_produces_a_visit(client, detectable_camera, config, ctx):
    """The chain, asserted at its end: a row in the timeline.

    A visit is written only when a *track ends*, so this waits out the whole
    object lifetime rather than just the first detection — which is why the
    budget here is minutes rather than seconds.
    """
    camera_id = detectable_camera["id"]
    require_tier0_calibrated(config, ctx)

    visits = eventually(
        lambda: _visits(client, camera_id),
        budget=BUDGETS.VISIT_APPEARS,
        describe=f"Tier-0 to record a visit for camera {camera_id}",
    )

    assert visits, "no visits were recorded"
    first = visits[0]
    assert first["camera_id"] == camera_id
    assert first["label"], f"the visit carries no object label: {first}"
    assert first["started_at"], f"the visit has no start time: {first}"


def test_a_visit_carries_a_usable_evidence_image(client, detectable_camera, config, ctx):
    """Evidence must be fetchable, not merely referenced.

    A row claiming ``has_evidence`` while the file is missing is worse than no
    evidence at all: the UI shows a broken frame and the operator cannot tell
    whether the system saw nothing or lost the picture. So this fetches the
    bytes and checks they are a real JPEG.
    """
    camera_id = detectable_camera["id"]
    require_tier0_calibrated(config, ctx)

    visit = eventually(
        lambda: next(
            (v for v in _visits(client, camera_id) if v.get("has_evidence")), None
        ),
        budget=BUDGETS.VISIT_APPEARS,
        describe=f"a visit with an evidence image on camera {camera_id}",
    )

    response = client.get(routes.EVENT_EVIDENCE(visit["id"]))

    assert response.content, "the evidence response body is empty"
    assert response.content[:2] == b"\xff\xd8", (
        "the evidence image is not a JPEG (no SOI marker); "
        f"content-type={response.headers.get('content-type')!r}"
    )


def test_visits_are_scoped_to_the_camera_that_saw_them(client, detectable_camera, config, ctx):
    """Filtering by camera must actually filter.

    Cheap to assert and easy to regress: the query is scoped by the caller's
    visible cameras as well as the filter, so a mistake here leaks one
    camera's history into another's timeline.
    """
    camera_id = detectable_camera["id"]
    require_tier0_calibrated(config, ctx)

    visits = eventually(
        lambda: _visits(client, camera_id),
        budget=BUDGETS.VISIT_APPEARS,
        describe=f"Tier-0 to record a visit for camera {camera_id}",
    )

    assert visits
    stray = [v for v in visits if v["camera_id"] != camera_id]
    assert not stray, f"visits from other cameras leaked into the filter: {stray[:3]}"


def test_the_detector_is_actually_running(client, config, ctx, detectable_camera):
    """Tier-0 reports itself healthy while footage is flowing.

    Its ``/health`` is deliberately function-based rather than a liveness
    ping: it goes unhealthy when the detector has degraded to the stub, when
    frames have gone stale, or when a configured bus is disconnected. Every
    silent-blindness incident on record was a process that was up and doing
    nothing, so "the container is running" is not the question worth asking.
    """
    import httpx

    require_tier0_calibrated(config, ctx)

    response = eventually(
        lambda: httpx.get(f"{config.detect.rstrip('/')}/health", timeout=10),
        until=lambda resp: resp.status_code == 200,
        budget=BUDGETS.PIPELINE_READY,
        describe="the Tier-0 pipeline to report itself healthy",
    )

    assert response.status_code == 200, (
        "Tier-0 reports unhealthy while a camera is publishing. It is up but "
        f"not doing its job: {response.text[:400]}"
    )
