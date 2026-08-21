# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the on-demand best-frame store (no cv2 — encode is injected)."""
from __future__ import annotations

from detect_pipeline.bestframe import BestFrameStore


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _store(**kw):
    # inject a fake encoder so tests never touch cv2 — record encode calls
    calls = {"n": 0}

    def enc(crop, q):
        calls["n"] += 1
        return b"JPEG:" + bytes(str(crop).encode()[:4])

    clk = _Clock()
    s = BestFrameStore(_clock=clk, _encode=enc, **kw)
    return s, clk, calls


def test_put_get_encodes_lazily_and_caches():
    s, _clk, calls = _store()
    s.put("cam1", 7, ["cropA"])
    assert calls["n"] == 0                    # put does not encode
    jpeg = s.get_jpeg("cam1", 7)
    assert jpeg is not None and calls["n"] == 1
    s.get_jpeg("cam1", 7)                     # second fetch uses the cache
    assert calls["n"] == 1


def test_same_crop_object_does_not_reencode():
    s, _clk, calls = _store()
    crop = ["A"]
    s.put("cam1", 1, crop)
    s.get_jpeg("cam1", 1)                     # encodes once
    s.put("cam1", 1, crop)                    # same object -> just a recency touch
    s.get_jpeg("cam1", 1)
    assert calls["n"] == 1
    s.put("cam1", 1, ["B"])                   # a NEW best crop -> cache reset
    s.get_jpeg("cam1", 1)
    assert calls["n"] == 2


def test_missing_and_unknown_return_none():
    s, _clk, _calls = _store()
    assert s.get_jpeg("cam1", 99) is None
    assert s.latest_jpeg("cam1") is None


def test_latest_returns_most_recently_touched_track():
    s, clk, _calls = _store()
    clk.t = 1.0
    s.put("cam1", 1, ["old"])
    clk.t = 2.0
    s.put("cam1", 2, ["new"])
    assert s.latest_jpeg("cam1") == s.get_jpeg("cam1", 2)


def test_age_eviction():
    s, clk, _calls = _store(max_age_s=10.0)
    clk.t = 0.0
    s.put("cam1", 1, ["A"])
    clk.t = 20.0                              # older than max_age
    assert s.get_jpeg("cam1", 1) is None
    assert len(s) == 0


def test_count_cap_evicts_oldest():
    s, clk, _calls = _store(max_entries=2)
    for i, t in enumerate((1.0, 2.0, 3.0)):
        clk.t = t
        s.put("cam1", i, [f"c{i}"])
    assert len(s) == 2                        # oldest (track 0) evicted
    assert s.get_jpeg("cam1", 0) is None
    assert s.get_jpeg("cam1", 2) is not None
