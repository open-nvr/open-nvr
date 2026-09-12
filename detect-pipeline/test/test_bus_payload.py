# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""build_payload ships `matched` per track (EVENT_CONTRACTS.md).

A live overlay draws only matched tracks; presence consumers use both.
Additive: nothing else in the payload moves.
"""
from __future__ import annotations

from types import SimpleNamespace

from detect_pipeline.bus import SCHEMA, build_payload
from detect_pipeline.pipeline import FrameResult
from detect_pipeline.tracking import Track


def _frame():
    return SimpleNamespace(seq=7, ts=123.0, width=1920, height=1080)


def test_matched_rides_on_every_track():
    live = Track(id=1, label="car", box=(10, 10, 110, 110), score=0.9, matched_now=True)
    ghost = Track(id=2, label="car", box=(500, 20, 600, 120), score=0.8, matched_now=False)
    result = FrameResult(tracks=[live, ghost])
    p = build_payload("cam3", result, _frame())
    assert p["schema"] == SCHEMA
    by_id = {t["id"]: t for t in p["tracks"]}
    assert by_id[1]["matched"] is True
    assert by_id[2]["matched"] is False
    # The rest of the track shape is untouched.
    assert by_id[2]["box"] == [500, 20, 600, 120] and by_id[2]["score"] == 0.8


def test_matched_defaults_true_for_a_track_without_the_flag():
    """A track object that predates the field entirely must not read as
    coasting — that would make an older producer's frames go dark.
    (A dataclass instance can't lose a class-defaulted attribute, so the
    stand-in is a plain object with no such attribute at all.)"""
    t = SimpleNamespace(id=1, label="car", box=(0, 0, 10, 10), score=0.5,
                        stationary=False)
    p = build_payload("cam3", FrameResult(tracks=[t]), _frame())
    assert p["tracks"][0]["matched"] is True


def test_a_fresh_track_object_defaults_to_coasting_until_matched():
    """The dataclass default is False on purpose: a Track nobody has
    matched yet is not 'seen this frame'. Only _spawn/_match set True."""
    t = Track(id=1, label="car", box=(0, 0, 10, 10), score=0.5)
    p = build_payload("cam3", FrameResult(tracks=[t]), _frame())
    assert p["tracks"][0]["matched"] is False
