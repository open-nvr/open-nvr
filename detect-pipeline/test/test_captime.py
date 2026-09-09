# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture time — the one timestamp a plate read is dated by (#451).

The failure these exist for: nothing downstream could say WHEN a vehicle
was seen, because frames carry a monotonic stamp that means nothing
outside the pipeline process. So every hop stamped its own arrival
instead, and one plate read ended up displayed with a different time on
every page — drifting further apart the more OCR backlog there was.
"""
from __future__ import annotations

import numpy as np

from detect_pipeline.captime import capture_wall
from detect_pipeline.platecands import CandidateRing


# ── monotonic -> wall ──────────────────────────────────────────────


def test_capture_wall_dates_a_frame_by_its_age():
    # A frame read 2s ago is 2s before now, whatever the two clocks'
    # absolute values are (monotonic's epoch is arbitrary by contract).
    wall = capture_wall(1_000.0, _mono=lambda: 1_002.0, _wall=lambda: 5_000.0)
    assert wall == 4_998.0


def test_capture_wall_is_immune_to_clock_offset_not_just_drift():
    # The same age gives the same answer no matter how far apart the two
    # clocks' origins are — this is why it measures age rather than
    # storing an anchor at stream start.
    a = capture_wall(10.0, _mono=lambda: 11.0, _wall=lambda: 1_700_000_000.0)
    b = capture_wall(9_999_990.0, _mono=lambda: 9_999_991.0,
                     _wall=lambda: 1_700_000_000.0)
    assert a == b == 1_699_999_999.0


def test_capture_wall_of_the_frame_being_read_now_is_now():
    assert capture_wall(50.0, _mono=lambda: 50.0, _wall=lambda: 123.0) == 123.0


# ── the early attempt carries the LOOK's time, not the post's ──────


class _FakePoster:
    def __init__(self):
        self.submitted = []

    def submit(self, attempt):
        self.submitted.append(attempt)
        return True


class _FakeTrack:
    def __init__(self, id, label="car", confirmed=True, ring=None):
        self.id = id
        self.label = label
        self.confirmed = confirmed
        self.plate_ring = ring


def _ring_at(ts, score=100.0):
    ring = CandidateRing(min_gap_s=0.0)
    ring.offer(ts, score, np.zeros((8, 8, 3), np.uint8))
    return ring


def test_attempt_is_dated_by_the_crop_not_by_the_submission():
    """The whole point: an attempt queued behind others must still say
    when its picture was taken."""
    from detect_pipeline.plate_attempts import EarlyPlateAttempts

    poster = _FakePoster()
    ea = EarlyPlateAttempts(
        poster, "cam1", nvr_camera_id=1, clock=lambda: 500.0,
        # The candidate was captured at monotonic 480; "now" is 500, so
        # the look is 20s old and its wall time is 20s before wall-now.
        capture_clock=lambda ts: 1_000.0 + (ts - 480.0),
    )
    assert ea.observe([_FakeTrack(1, ring=_ring_at(480.0))]) == 1
    assert poster.submitted[0].ts == 1_000.0


def test_two_looks_of_one_car_are_dated_apart():
    from detect_pipeline.plate_attempts import EarlyPlateAttempts

    poster = _FakePoster()
    # The policy's own clock has to advance past min_retry_gap_s, or the
    # second look never earns an attempt to be dated.
    ticks = iter([100.0, 160.0])
    ea = EarlyPlateAttempts(
        poster, "cam1", nvr_camera_id=1, max_attempts=2,
        clock=lambda: next(ticks), capture_clock=lambda ts: ts,
    )
    ea.observe([_FakeTrack(1, ring=_ring_at(10.0, score=10.0))])
    # A much better look later in the track earns the second attempt.
    ea.observe([_FakeTrack(1, ring=_ring_at(40.0, score=900.0))])
    assert [a.ts for a in poster.submitted] == [10.0, 40.0]
