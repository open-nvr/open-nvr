# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for Tier-1 dispatch (#10): routing, gating, and the KAI-C client."""
from __future__ import annotations

import base64
import threading
import time

import numpy as np

from detect_pipeline.dispatch import (
    DispatchRouter,
    KaicDispatcher,
    build_infer_body,
    dispatch_escalations,
)
from detect_pipeline.gate import GateDecision, GateResult
from detect_pipeline.tracking import Track


def _track(tid=1, label="person", crop=True):
    t = Track(id=tid, label=label, box=(0, 0, 4, 4), score=0.9, confirmed=True)
    if crop:
        t.best_crop = np.zeros((4, 4, 3), np.uint8)
    return t


class _FakeDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, cam, adapter, crop, track):
        self.calls.append((cam, adapter, track.id))


# ── routing ──

def test_router_default_and_custom():
    r = DispatchRouter()
    assert r.route("person") == ["caption"]        # default: caption on person/vehicle
    assert r.route("car") == ["caption"]
    assert r.route("cat") == []                    # unlisted -> default (empty)
    r2 = DispatchRouter(routes={"cat": ["my-adapter"]}, default=["caption"])
    assert r2.route("cat") == ["my-adapter"]       # custom model = one row
    assert r2.route("dog") == ["caption"]          # falls back to default


def test_build_infer_body_is_contract_v1():
    body = build_infer_body("caption", b"\xff\xd8jpeg", {"camera_id": "c", "label": "person"})
    assert body["task"] == "caption" and body["camera_id"] == "c"
    assert base64.b64decode(body["frame_b64"]) == b"\xff\xd8jpeg"


# ── gating: enforce dispatches, shadow/off do not ──

def test_enforce_dispatches_routed_adapters():
    fd = _FakeDispatcher()
    gr = GateResult(decisions=[
        GateDecision(1, "person", True, "new_track", False),
        GateDecision(2, "cat", True, "new_track", False),   # routes to nothing
    ], shadow=False)
    n = dispatch_escalations("cam", [_track(1, "person"), _track(2, "cat")], gr, DispatchRouter(), fd)
    assert n == 1 and fd.calls == [("cam", "caption", 1)]


def test_shadow_dispatches_nothing():
    fd = _FakeDispatcher()
    gr = GateResult(decisions=[GateDecision(1, "person", True, "new_track", True)], shadow=True)
    assert dispatch_escalations("cam", [_track(1)], gr, DispatchRouter(), fd) == 0
    assert fd.calls == []


def test_skips_track_without_best_crop():
    fd = _FakeDispatcher()
    gr = GateResult(decisions=[GateDecision(1, "person", True, "new_track", False)], shadow=False)
    assert dispatch_escalations("cam", [_track(1, crop=False)], gr, DispatchRouter(), fd) == 0


def test_no_dispatcher_is_a_noop():
    gr = GateResult(decisions=[GateDecision(1, "person", True, "new_track", False)], shadow=False)
    assert dispatch_escalations("cam", [_track(1)], gr, DispatchRouter(), None) == 0


# ── the KAI-C client ──

def test_router_does_not_mutate_module_default():
    from detect_pipeline.dispatch import DEFAULT_ROUTES
    r = DispatchRouter()
    r.routes["person"].append("face")              # mutate the instance
    assert DEFAULT_ROUTES["person"] == ["caption"]  # module global untouched


def test_dispatcher_drops_under_backpressure_and_balances_semaphore():
    # max_inflight=1: the first dispatch holds the slot; the second must be dropped
    # (not queued, not deadlocked) — pinning the acquire/release balance.
    posts: list = []
    gate = threading.Event()

    def blocking_post(url, body, api_key, timeout):
        posts.append(url)
        gate.wait(1.0)

    d = KaicDispatcher("http://kaic:8100", task="caption", max_inflight=1, http_post=blocking_post)
    crop, t = np.zeros((4, 4, 3), np.uint8), _track(1)
    d.dispatch("cam", "caption", crop, t)          # takes the one slot (in flight)
    for _ in range(200):                            # wait until it's actually posting
        if posts:
            break
        time.sleep(0.005)
    from detect_pipeline.metrics import metrics
    metrics.reset()
    d.dispatch("cam", "caption", crop, t)          # no slot -> dropped
    time.sleep(0.05)
    assert len(posts) == 1                          # second dropped, semaphore not leaked
    assert metrics.value("tier1_dispatch_dropped_total", {"camera": "cam", "adapter": "caption"}) == 1
    gate.set()
    d.close()
    metrics.reset()


def test_dispatcher_emits_call_metrics():
    from detect_pipeline.metrics import metrics
    metrics.reset()
    d = KaicDispatcher("http://k", task="caption", http_post=lambda *a: None)
    d._run("cam_3", "caption", np.zeros((4, 4, 3), np.uint8), _track(1, "person"))
    assert metrics.value("tier1_dispatch_total", {"camera": "cam_3", "adapter": "caption"}) == 1
    assert metrics.value("tier1_dispatch_errors_total", {"camera": "cam_3", "adapter": "caption"}) == 0
    metrics.reset()


def test_kaic_dispatcher_posts_governed_infer():
    posted = {}

    def fake_post(url, body, api_key, timeout):
        posted.update(url=url, body=body, key=api_key)

    d = KaicDispatcher("http://kaic:8100/", api_key="tok", task="caption", http_post=fake_post)
    d._run("cam_3", "caption", np.zeros((4, 4, 3), np.uint8), _track(7, "person"))
    assert posted["url"] == "http://kaic:8100/api/v1/infer/caption"
    assert posted["body"]["task"] == "caption" and "frame_b64" in posted["body"]
    assert posted["body"]["camera_id"] == "cam_3" and posted["body"]["track_id"] == 7
    assert posted["key"] == "tok"


# ── fairness: one busy camera must not take every permit ────────────

def _blank():
    return np.zeros((4, 4, 3), np.uint8)


def test_one_camera_cannot_consume_every_dispatch_permit():
    """dispatch_escalations fires once per escalated track in a tight loop,
    so a single camera with several escalating tracks took all permits in one
    frame and locked out the whole fleet until they returned."""
    from detect_pipeline.dispatch import KaicDispatcher

    started, release = [], threading.Event()

    def slow_post(url, body, key, timeout):
        started.append(1)
        release.wait(timeout=10)

    d = KaicDispatcher("http://kaic", max_inflight=4, http_post=slow_post)
    try:
        # A busy camera may run nearly flat out, but never take the last slot.
        for i in range(8):                       # one greedy camera
            d.dispatch("cam-busy", "caption", _blank(), _track(i))
        for _ in range(200):
            if len(started) >= 2:
                break
            time.sleep(0.01)

        assert len(started) <= 3, f"cam-busy took {len(started)} of 4 permits"
        d.dispatch("cam-quiet", "caption", _blank(), _track(99))
        for _ in range(200):
            if len(started) >= 3:
                break
            time.sleep(0.01)
        assert len(started) >= 3, "quiet camera was starved by the busy one"
    finally:
        release.set()
        d.close()


def test_camera_slots_are_returned_after_completion():
    from detect_pipeline.dispatch import KaicDispatcher

    d = KaicDispatcher("http://kaic", max_inflight=4, http_post=lambda *a, **k: None)
    try:
        for i in range(10):
            d.dispatch("cam1", "caption", _blank(), _track(i))
            time.sleep(0.02)
        time.sleep(0.2)
        assert d._cam_inflight.get("cam1", 0) == 0, d._cam_inflight
    finally:
        d.close()
