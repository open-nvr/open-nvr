# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the detector-adapter interface + tensor shaping."""
from __future__ import annotations

import time

import numpy as np
import pytest

from detect_pipeline.detector import (
    RawDetection,
    StubDetector,
    crop_and_resize,
    detections_to_frame,
    to_bgr,
)
from detect_pipeline.ffmpeg_presets import frame_size_bytes

W, H = 64, 48


def test_to_bgr_shape():
    data = bytes(frame_size_bytes(W, H))          # a valid-sized I420 buffer
    bgr = to_bgr(data, W, H)
    assert bgr.shape == (H, W, 3)


def test_crop_and_resize_to_model_input():
    bgr = np.zeros((H, W, 3), np.uint8)
    out = crop_and_resize(bgr, (10, 5, 40, 35), out_w=320, out_h=320)
    assert out.shape == (320, 320, 3)


def test_crop_rejects_empty_region():
    bgr = np.zeros((H, W, 3), np.uint8)
    with pytest.raises(ValueError):
        crop_and_resize(bgr, (10, 10, 10, 10), 320, 320)


def test_detections_map_from_crop_to_full_frame():
    region = (100, 200, 200, 300)                 # 100×100 region at (100,200)
    raws = [RawDetection("person", 0.9, (0.5, 0.5, 1.0, 1.0))]
    dets = detections_to_frame(raws, region)
    assert len(dets) == 1
    d = dets[0]
    assert d.label == "person" and d.score == 0.9
    # (0.5,0.5)-(1.0,1.0) of a 100px region at offset (100,200)
    assert d.box == (150, 250, 200, 300)


def test_stub_detector_returns_nothing():
    assert StubDetector().detect(np.zeros((320, 320, 3), np.uint8)) == []


# ── DetectorPool: cap resident models without sharing one concurrently ──

def test_pool_never_hands_one_detector_to_two_threads_at_once():
    """The invariant that forced one detector per worker: cv2.dnn.Net.forward
    is not safe to call concurrently on the same Net. Pooling is only
    legitimate if a borrowed detector is exclusively held."""
    import threading

    from detect_pipeline.detector import DetectorPool

    violations = []

    class _Det:
        def __init__(self):
            self.busy = False

        def detect(self, crop):
            if self.busy:                      # someone else is inside this instance
                violations.append(1)
            self.busy = True
            time.sleep(0.001)
            self.busy = False
            return []

    pool = DetectorPool(_Det, 4)
    threads = [threading.Thread(target=lambda: [pool.detect(None) for _ in range(40)])
               for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not violations, f"{len(violations)} concurrent uses of one detector"


def test_pool_caps_instances_under_concurrency():
    import threading

    from detect_pipeline.detector import DetectorPool

    class _Det:
        def detect(self, crop):
            time.sleep(0.002)
            return []

    pool = DetectorPool(_Det, 3)
    threads = [threading.Thread(target=lambda: [pool.detect(None) for _ in range(20)])
               for _ in range(24)]                # 24 "cameras", cap of 3
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pool.created <= 3, f"built {pool.created} detectors past the cap"


def test_pool_grows_lazily_so_small_installs_are_unaffected():
    """Below the cap nothing changes — a 2-camera box must not allocate 8
    models just because the cap allows it."""
    from detect_pipeline.detector import DetectorPool

    made = []
    pool = DetectorPool(lambda: made.append(1) or _Rec(), 8)
    for _ in range(50):
        pool.detect(None)                      # sequential: one is enough
    assert pool.created == 1


class _Rec:
    def detect(self, crop):
        return []


def test_pool_does_not_recirculate_a_degraded_detector():
    """The factory degrades to StubDetector when a model fails to load. Under
    one-per-worker that blinded ONE camera; in a shared pool the stub would
    circulate forever, so a rotating share of every camera's frames would
    silently detect nothing — and /health samples a separate probe instance,
    so it would keep reporting the real model."""
    from detect_pipeline.detector import DetectorPool, StubDetector

    calls = {"n": 0}

    def flaky_factory():
        calls["n"] += 1
        return StubDetector() if calls["n"] == 1 else _Rec()   # first load fails

    pool = DetectorPool(flaky_factory, 4)
    assert pool.detect(None) == []          # got the stub this time
    for _ in range(10):
        pool.detect(None)
    # The stub was discarded rather than parked in the free list.
    assert all(not isinstance(d, StubDetector) for d in pool._free), pool._free
    assert pool._degraded >= 1


def test_pool_created_counts_only_what_was_built():
    """`_created += 1` used to commit before the factory ran, so a failed load
    inflated it forever and real capacity shrank below max_size unnoticed."""
    from detect_pipeline.detector import DetectorPool

    boom = {"n": 0}

    def failing_factory():
        boom["n"] += 1
        raise RuntimeError("model load failed")

    pool = DetectorPool(failing_factory, 4)
    for _ in range(5):
        try:
            pool.detect(None)
        except RuntimeError:
            pass
    assert pool.created == 0, pool.created
