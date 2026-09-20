# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plate reading, from a vehicle in frame to a number on the Vehicles page.

The longest chain in the product, and the one that fails most quietly. A plate
read needs *all* of: Tier-0 producing a vehicle visit with an evidence crop,
the ``fast_plate_ocr`` adapter registered with KAI-C, and footage where the
plate is genuinely legible. Miss any one and the Vehicles page is simply
empty — no error, no log line.

The tests are split by what they can prove deterministically:

* **Ingest** (``media``) drives core's internal door directly with a synthetic
  visit. No Tier-0, no OCR, no real footage — so it runs anywhere and isolates
  the transport: does a posted visit become a row, and does its evidence come
  back as the bytes that went in? When this fails, nothing further is worth
  debugging.

* **Reading** (``lpr``) is the real chain on real footage. It cannot be faked:
  OCR needs a plate that is legible in a paused frame, which no generated clip
  provides. It skips, loudly, when the rig has only synthetic clips.

The adapter guard runs first in the ``lpr`` tests on purpose. KAI-C holds its
adapter registry in memory, so restarting core silently empties it, and the
only symptom is ``plate_text`` staying null forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness import routes
from harness.budgets import BUDGETS
from harness.images import jpeg, jpeg_b64
from harness.waiting import eventually

# ---------------------------------------------------------------------------
# Ingest — deterministic, no footage required
# ---------------------------------------------------------------------------
@pytest.mark.media
def test_a_posted_visit_becomes_a_readable_event(client, sandbox, config):
    """Core's internal door is how every visit in the product is created.

    Tier-0 posts here at track end; this test posts the same payload by hand.
    Doing it directly separates "the pipeline saw nothing" from "the pipeline
    saw something and core dropped it" — two failures that look identical from
    the timeline and need completely different fixes.
    """
    camera = client.create_camera(
        label="ingest",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('nostream')}",
        ip_address=config.fakecam_ip,
    )
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    track_id = sandbox.name("track")

    client.post(
        routes.INTERNAL_EVENTS,
        internal=True,
        json_body={
            "camera_id": camera["id"],
            "label": "car",
            "score": 0.91,
            "track_id": track_id,
            "started_at": started.isoformat(),
            "ended_at": (started + timedelta(seconds=12)).isoformat(),
            "evidence_jpeg_b64": jpeg_b64(320, 240),
        },
    )

    visit = eventually(
        lambda: next(
            (
                event
                for event in client.json(
                    routes.EVENTS, params={"camera_id": camera["id"], "limit": 20}
                )["events"]
                if event.get("track_id") == track_id
            ),
            None,
        ),
        budget=BUDGETS.QUICK,
        describe=f"the posted visit {track_id} to appear in the timeline",
    )

    assert visit["label"] == "car"
    assert visit["camera_id"] == camera["id"]
    assert visit["has_evidence"], (
        "the visit was stored without its evidence image, so the timeline has "
        "a row nobody can look at"
    )


@pytest.mark.media
def test_stored_evidence_is_the_image_that_was_posted(client, sandbox, config):
    """Evidence must survive the round trip byte-for-byte.

    Evidence is content-addressed on the way in and re-served on the way out.
    A row that says it has evidence but serves back something re-encoded — or
    nothing — is worse than no evidence at all: it is the record an operator
    would rely on.
    """
    camera = client.create_camera(
        label="evidence",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('nostream')}",
        ip_address=config.fakecam_ip,
    )
    started = datetime.now(timezone.utc) - timedelta(seconds=20)
    track_id = sandbox.name("evid")
    original = jpeg(320, 240)

    client.post(
        routes.INTERNAL_EVENTS,
        internal=True,
        json_body={
            "camera_id": camera["id"],
            "label": "person",
            "track_id": track_id,
            "started_at": started.isoformat(),
            "ended_at": (started + timedelta(seconds=5)).isoformat(),
            "evidence_jpeg_b64": jpeg_b64(320, 240),
        },
    )

    visit = eventually(
        lambda: next(
            (
                event
                for event in client.json(
                    routes.EVENTS, params={"camera_id": camera["id"], "limit": 20}
                )["events"]
                if event.get("track_id") == track_id
            ),
            None,
        ),
        budget=BUDGETS.QUICK,
        describe=f"the posted visit {track_id} to appear",
    )

    served = client.get(routes.EVENT_EVIDENCE(visit["id"])).content

    assert served == original, (
        f"the evidence image changed in storage: posted {len(original)} bytes, "
        f"got {len(served)} back"
    )


# ---------------------------------------------------------------------------
# Reading — the real chain, on real footage
# ---------------------------------------------------------------------------
@pytest.mark.lpr
def test_a_vehicle_visit_gets_a_plate_read(client, detectable_camera, plate_ocr, config, ctx):
    """The whole chain: vehicle in frame, plate on the timeline.

    Every link is silent when it breaks, so the guard runs first and the wait
    is generous: OCR is a background task behind a semaphore, and its result
    reaches the row through a NATS round trip.
    """
    camera_id = detectable_camera["id"]

    visit = eventually(
        lambda: next(
            (
                event
                for event in client.json(
                    routes.EVENTS,
                    params={"camera_id": camera_id, "has_plate": True, "limit": 50},
                )["events"]
                if event.get("plate_text")
            ),
            None,
        ),
        budget=BUDGETS.PLATE_ENRICHED + BUDGETS.VISIT_APPEARS,
        describe=f"a plate to be read on camera {camera_id}",
    )

    plate = visit["plate_text"].strip()
    assert plate, "the visit is flagged as having a plate but the text is empty"
    assert len(plate) >= 4, f"implausibly short plate text: {plate!r}"


@pytest.mark.lpr
def test_the_plate_is_paired_with_the_frame_it_was_read_from(
    client, detectable_camera, plate_ocr, config, ctx
):
    """Only ``plate_frame_path`` reliably shows the vehicle the plate belongs to.

    Tier-0 can merge two vehicles into one track, which pairs one car's plate
    with another car's best-frame photo. The evidence crop is therefore *not*
    trustworthy as proof of which vehicle was read — the plate frame is. An
    operator shown the wrong car next to a plate number is being actively
    misled, so a plate read must carry the frame it came from.
    """
    camera_id = detectable_camera["id"]

    visit = eventually(
        lambda: next(
            (
                event
                for event in client.json(
                    routes.EVENTS,
                    params={"camera_id": camera_id, "has_plate": True, "limit": 50},
                )["events"]
                if event.get("plate_text")
            ),
            None,
        ),
        budget=BUDGETS.PLATE_ENRICHED + BUDGETS.VISIT_APPEARS,
        describe=f"a plate to be read on camera {camera_id}",
    )

    frame = client.get(routes.EVENT_PLATE_FRAME(visit["id"]), expect=None)

    assert frame.status_code == 200, (
        f"a plate was read ({visit['plate_text']!r}) but the frame it came "
        f"from is not retrievable (HTTP {frame.status_code}). Without it there "
        "is no way to tell which vehicle the number belongs to."
    )
    assert frame.content[:2] == b"\xff\xd8", "the plate frame is not a JPEG"


@pytest.mark.lpr
def test_plate_reads_reach_the_reporting_surfaces(
    client, detectable_camera, plate_ocr, config, ctx
):
    """The Vehicles page is built from aggregates, not from raw events.

    A plate can be on the row and still be missing from every view an operator
    actually opens, so the aggregates are worth asserting separately.
    """
    camera_id = detectable_camera["id"]

    eventually(
        lambda: next(
            (
                event
                for event in client.json(
                    routes.EVENTS,
                    params={"camera_id": camera_id, "has_plate": True, "limit": 50},
                )["events"]
                if event.get("plate_text")
            ),
            None,
        ),
        budget=BUDGETS.PLATE_ENRICHED + BUDGETS.VISIT_APPEARS,
        describe=f"a plate to be read on camera {camera_id}",
    )

    stats = client.json(routes.EVENTS_PLATE_STATS)
    summary = client.json(routes.EVENTS_PLATE_SUMMARY)
    sessions = client.json(routes.EVENTS_PLATE_SESSIONS)

    assert stats, "plate statistics came back empty after a successful read"
    assert summary, "the plate summary came back empty after a successful read"
    assert sessions is not None, "plate sessions returned nothing at all"
