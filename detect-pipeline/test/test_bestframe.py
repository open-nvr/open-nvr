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


# ── fleet fairness: one busy camera must not starve the quiet ones ──

def test_a_busy_camera_cannot_evict_a_quiet_cameras_best_frame():
    """The regression: the count cap was GLOBAL and recency was driven by put
    rate, so a busy/high-fps camera continuously evicted quiet cameras and
    "best frame for cam-quiet" came back empty on a real fleet."""
    s, clk, _calls = _store(max_entries=8, max_per_camera=4)

    clk.t = 1.0
    s.put("cam-quiet", "t1", ["quiet-best"])          # one frame, then silence

    for i in range(200):                              # a busy neighbour floods
        clk.t = 2.0 + i * 0.01
        s.put("cam-busy", f"t{i}", [f"busy{i}"])

    assert s.get_jpeg("cam-quiet", "t1") is not None, \
        "quiet camera's best frame was evicted by a busy neighbour"
    # ...and the busy camera is held to its own quota rather than the store.
    assert len(s) <= 8


def test_per_camera_quota_is_enforced_independently():
    s, clk, _calls = _store(max_entries=100, max_per_camera=3)
    for cam in ("a", "b"):
        for i in range(10):
            clk.t = 1.0 + i
            s.put(cam, f"t{i}", [f"{cam}{i}"])
    assert len(s) == 6                                # 3 per camera, both kept
    for cam in ("a", "b"):                            # newest survive, oldest go
        assert s.get_jpeg(cam, "t9") is not None
        assert s.get_jpeg(cam, "t0") is None


def test_global_backstop_takes_from_the_largest_bucket():
    s, clk, _calls = _store(max_entries=4, max_per_camera=10)
    clk.t = 1.0
    s.put("small", "only", ["keep-me"])
    for i in range(9):
        clk.t = 2.0 + i
        s.put("big", f"t{i}", [f"b{i}"])
    # The single-entry camera survives; the hog is trimmed.
    assert s.get_jpeg("small", "only") is not None
    assert len(s) <= 4


def test_latest_jpeg_is_scoped_to_its_own_camera():
    s, clk, _calls = _store()
    clk.t = 1.0
    s.put("cam1", "a", ["one"])
    clk.t = 5.0
    s.put("cam2", "b", ["two"])                       # newer, different camera
    assert s.latest_jpeg("cam1") == s.get_jpeg("cam1", "a")
    assert s.latest_jpeg("cam2") == s.get_jpeg("cam2", "b")


def test_latest_jpeg_skips_expired_entries():
    s, clk, _calls = _store(max_age_s=10.0)
    clk.t = 0.0
    s.put("cam1", "old", ["stale"])
    clk.t = 5.0
    s.put("cam1", "new", ["fresh"])
    clk.t = 12.0                                       # "old" is now expired
    assert s.latest_jpeg("cam1") == s.get_jpeg("cam1", "new")


def test_per_camera_quota_is_reachable_on_a_real_fleet():
    """A hard-coded global 256 silently overruled the configured per-camera
    quota: 20 cameras x 16 = 320 > 256, so from camera 17 onward every put
    evicted somebody and DETECT_BESTFRAME_PER_CAMERA became a fiction."""
    s, clk, _calls = _store(max_per_camera=16)          # no global cap
    clk.t = 100.0                                      # inside max_age for all
    for cam in range(20):
        for t in range(16):
            s.put(f"cam{cam}", f"t{t}", [f"{cam}-{t}"])
    assert len(s) == 20 * 16, len(s)
    # ...and every camera really kept its full share.
    for cam in (0, 9, 19):
        assert s.get_jpeg(f"cam{cam}", "t15") is not None


def test_explicit_global_cap_is_still_honoured():
    s, clk, _calls = _store(max_entries=10, max_per_camera=16)
    for cam in range(5):
        for t in range(8):
            clk.t += 1
            s.put(f"cam{cam}", f"t{t}", [f"{cam}-{t}"])
    assert len(s) <= 10
