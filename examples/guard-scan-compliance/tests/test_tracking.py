# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Identity across frames.

This exists because of a real regression. The prototype got tracking
free from the pose model; the ported app calls an adapter that only
DETECTS, and numbering detections in arrival order looked fine — until
a clip that scored a clean 100% in the prototype came out as "partial,
missing Back" on the platform. Detections arrive ordered by confidence,
that order flips between frames, and the guard and the customer swapped
identities several times a second.

Nothing raised an error. The verdict was simply wrong.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guard_scan.tracking import Tracker, iou  # noqa: E402


def box(cx, cy, w=60, h=180):
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def test_a_person_who_moves_a_little_keeps_their_id():
    t = Tracker()
    first = t.update([box(100, 200)], 1000.0)
    second = t.update([box(112, 205)], 1000.1)
    assert first == second


def test_two_people_keep_their_own_ids_when_the_detection_order_flips():
    """The regression itself. The adapter returns whichever person it is
    most confident about first, and that changes frame to frame."""
    t = Tracker()
    guard, customer = box(100, 200), box(400, 200)
    ids = t.update([guard, customer], 1000.0)
    guard_id, customer_id = ids

    # Same two people, same places — reported the other way round.
    flipped = t.update([box(402, 201), box(102, 202)], 1000.1)
    assert flipped == [customer_id, guard_id], (
        "identity followed the list position instead of the person")


def test_a_new_person_gets_a_new_id():
    t = Tracker()
    (first,) = t.update([box(100, 200)], 1000.0)
    ids = t.update([box(103, 201), box(600, 200)], 1000.1)
    assert ids[0] == first
    assert ids[1] != first


def test_someone_briefly_hidden_keeps_their_id():
    """People walk in front of each other constantly. Retiring an id the
    moment a detection is missed would mint a new person — and a new
    screening — every time that happens."""
    t = Tracker()
    (who,) = t.update([box(100, 200)], 1000.0)
    t.update([], 1000.4)                       # lost behind someone
    t.update([], 1000.8)
    again = t.update([box(108, 203)], 1001.1)
    assert again == [who]


def test_an_id_is_retired_once_they_are_really_gone():
    t = Tracker()
    (who,) = t.update([box(100, 200)], 1000.0)
    t.update([], 1002.0)                       # well past max_gap_s
    later = t.update([box(100, 200)], 1002.1)
    assert later != [who], "a stale id was handed to a new arrival"


def test_ids_are_never_reused():
    """A recycled id would graft one person's screening onto another's."""
    t = Tracker()
    seen = set()
    now = 1000.0
    for i in range(5):
        now += 3.0                             # each one leaves before the next
        (who,) = t.update([box(100 + i, 200)], now)
        assert who not in seen
        seen.add(who)


def test_a_big_stride_still_matches_by_distance():
    """At 10 fps a brisk walk can clear the previous box entirely, and
    overlap then says nothing — so the second pass asks "is that them,
    one step on?" instead."""
    t = Tracker()
    (who,) = t.update([box(100, 200)], 1000.0)
    assert iou(box(100, 200), box(165, 200)) == 0.0      # no overlap at all
    again = t.update([box(165, 200)], 1000.1)
    assert again == [who]


def test_a_leap_across_the_room_is_somebody_else():
    t = Tracker()
    (who,) = t.update([box(100, 200)], 1000.0)
    other = t.update([box(900, 200)], 1000.1)
    assert other != [who]
