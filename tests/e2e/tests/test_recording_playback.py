# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Footage arrives, gets indexed, and can be played back and exported.

This is the product's core promise, and it spans more moving parts than any
other journey in the suite: MediaMTX writes a segment to disk, calls a webhook,
core resolves that file back to a camera and indexes it, and the playback API
serves it. A break anywhere in that chain looks identical from the UI — an
empty timeline — so unit tests on any single link cannot tell you it works.

These run on a **generated** clip. Recording does not care what is in the
picture, only that frames keep arriving, so there is no reason to spend real
footage (or its variability) here. That belongs to the detection tier.

Segment length is set to 10s for the E2E stack via ``RECORDING_SEGMENT_SECONDS``
(the product default is 60). Without that, every assertion below would carry a
full minute of dead waiting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness import routes
from harness.budgets import BUDGETS
from harness.waiting import eventually

pytestmark = pytest.mark.media


def _segments(client, camera_id: int) -> dict:
    return client.json(routes.RECORDINGS_SEGMENTS(camera_id))


def test_a_publishing_camera_produces_playable_segments(client, publishing_camera, ctx):
    """Footage is recorded and offered to the timeline with everything it needs.

    Note what this does *not* prove: the endpoint falls back to MediaMTX when
    the database has no rows, so a pass here means "footage exists and is
    described correctly", not "the segment-complete webhook indexed it".
    ``test_a_frame_can_be_extracted_from_past_footage`` is the one that proves
    the index — it is the only recordings endpoint without a fallback.
    """
    camera_id = publishing_camera["id"]

    day = eventually(
        lambda: _segments(client, camera_id),
        until=lambda payload: payload.get("segment_count", 0) > 0,
        budget=BUDGETS.SEGMENT_RECORDED,
        describe=f"a recorded segment to be indexed for camera {camera_id}",
    )

    assert day["segments"], "segment_count was positive but the list is empty"
    first = day["segments"][0]
    assert first.get("start"), f"segment carries no start instant: {first}"
    assert first.get("duration", 0) > 0, f"segment has no duration: {first}"
    assert first.get("playback_url"), (
        f"segment has no playback URL, so the timeline cannot play it: {first}"
    )


def test_recorded_footage_is_listed_for_playback(client, publishing_camera):
    """The playback list is what the UI's camera picker reads.

    Distinct from the segment timeline: ``/playback/list`` aggregates a day
    into one entry per camera path and is served from MediaMTX rather than the
    index, so it can be empty even when segments exist (and vice versa).
    """
    camera_id = publishing_camera["id"]
    path = _path_for(client, camera_id)

    listed = eventually(
        lambda: client.json(routes.RECORDINGS_PLAYBACK_LIST, params={"path": path}),
        until=lambda payload: bool(_entries(payload)),
        budget=BUDGETS.SEGMENT_RECORDED,
        describe=f"MediaMTX to list recorded footage for {path}",
    )

    assert _entries(listed), f"no playback entries for {path}: {listed}"


@pytest.mark.xfail(
    reason=(
        "ffmpeg is not installed in the opennvr-core image, so this endpoint "
        "returns 502 on every install. See the docstring."
    ),
)
def test_a_frame_can_be_extracted_from_past_footage(client, recorded_camera):
    """"What was happening at 3:14pm" — a real JPEG out of a recorded file.

    **Currently xfail: this is a real, shipped bug, not a flaky test.**

    ``_extract_recording_frame`` (server/routers/recordings.py) shells out to
    ``ffmpeg``, but the runtime stage of the root ``Dockerfile`` installs only
    supervisor, curl, gosu, libpq5 and a few X libraries — no ffmpeg. So the
    binary is absent from ``ghcr.io/open-nvr/core`` and every request here
    fails extraction and answers 502 "Could not extract frame".

    Confirmed on a clean E2E stack and on a normal deployment:

        docker exec opennvr_core sh -c 'command -v ffmpeg' -> nothing

    The endpoint powers "what was happening at 3:14pm" and the camera agent's
    ``describe_window``, which samples several frames across a window.

    Adding ffmpeg to the runtime stage should turn this XPASS, which is the
    signal to drop the xfail. The assertion below is deliberately the real
    one — it must start passing on its own merits, not be weakened to match
    the bug.
    """
    camera, segment = recorded_camera
    camera_id = camera["id"]
    instant = _instant_inside(segment)

    response = client.get(
        routes.RECORDINGS_FRAME,
        params={"camera_id": camera_id, "at": instant},
        expect=None,
    )

    assert response.status_code == 200, (
        f"frame extraction failed with {response.status_code}: "
        f"{response.text[:200]}"
    )
    assert response.content[:2] == b"\xff\xd8", (
        "the response is not a JPEG (no SOI marker); "
        f"content-type={response.headers.get('content-type')!r}"
    )


def test_a_clip_can_be_exported(client, recorded_camera):
    """Export is ticketed: mint, then download with the ticket.

    The two halves are deliberately separate in the product — the ticket is
    short-lived and single-use so a clip URL cannot be shared or replayed —
    and only exercising both together proves the pair actually works.
    """
    camera, segment = recorded_camera
    camera_id = camera["id"]
    # Stay well inside the recorded run: asking for more than exists is a
    # different behaviour, and not the one under test here.
    duration = max(1.0, min(4.0, float(segment["duration"]) - 1.0))

    ticket = client.post(
        routes.RECORDINGS_EXPORT_TICKET,
        params={
            "camera_id": camera_id,
            "start": segment["start"],
            "duration": duration,
        },
    ).json()

    token = ticket.get("ticket") or ticket.get("token")
    assert token, f"no ticket in the response: {ticket}"

    clip = client.get(routes.RECORDINGS_EXPORT, params={"ticket": token}, expect=None)
    assert clip.status_code == 200, (
        f"exporting with a freshly minted ticket failed "
        f"({clip.status_code}): {clip.text[:300]}"
    )
    assert len(clip.content) > 0, "the exported clip is empty"


def test_recording_statistics_account_for_the_footage(client, publishing_camera):
    """Storage reporting must reflect what is actually on disk.

    Operators size retention from these numbers, so a stats endpoint that
    stays at zero while files accumulate is its own kind of outage.
    """
    camera_id = publishing_camera["id"]
    eventually(
        lambda: _segments(client, camera_id),
        until=lambda payload: payload.get("segment_count", 0) > 0,
        budget=BUDGETS.SEGMENT_RECORDED,
        describe=f"a recorded segment to exist for camera {camera_id}",
    )

    stats = eventually(
        lambda: client.json(routes.RECORDINGS_STATS),
        until=lambda payload: bool(payload),
        budget=BUDGETS.QUICK,
        describe="recording statistics to be reported",
    )
    assert stats, "recording stats came back empty"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _instant_inside(segment: dict) -> str:
    """An instant a couple of seconds into a recorded run.

    Not the boundary: a seek exactly at the edge can legitimately land in the
    previous file, or in none at all.
    """
    start = datetime.fromisoformat(segment["start"].replace("Z", "+00:00"))
    return (start + timedelta(seconds=2)).astimezone(timezone.utc).isoformat()


def _path_for(client, camera_id: int) -> str:
    """The camera's MediaMTX path name, taken from the system rather than built.

    Path naming is ``cam-<id>`` or ``cam-<ip>`` depending on configuration, so
    constructing it here would make the test fail on a system set up the other
    way for no real reason.
    """
    status = client.json(routes.CAMERA_MEDIAMTX_STATUS(camera_id))
    path = (status.get("path_status") or {}).get("path")
    assert path, f"could not determine the MediaMTX path for camera {camera_id}"
    return path


def _entries(payload) -> list:
    """Playback entries out of whichever envelope the endpoint used."""
    if isinstance(payload, list):
        return payload
    for key in ("recordings", "items", "segments", "entries"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []
