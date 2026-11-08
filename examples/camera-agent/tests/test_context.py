# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the CameraContext (frame cache + event ring + NATS
event parser)."""
from __future__ import annotations

import asyncio
import time

import pytest

from context import (
    CameraContext,
    CameraSpec,
    EventRecord,
    _adapter_from_subject,
    _parse_inference_event,
    _summarise_event,
)
from frame_sources import FrameSourceError


class _StubFrameSource:
    """Records each fetch + returns the next pre-canned blob."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self.calls = 0

    def fetch(self) -> bytes:
        self.calls += 1
        if not self._frames:
            raise FrameSourceError("stub exhausted")
        return self._frames.pop(0)

    @property
    def camera_id(self) -> str:
        return "stub"


def _make_ctx(ttl: float = 2.0, ring_size: int = 32):
    spec = CameraSpec(camera_id="front-porch", frame_url="http://x", role="entrance")
    ctx = CameraContext(
        cameras=[spec], frame_cache_ttl_seconds=ttl, event_ring_size=ring_size
    )
    return ctx


# ── Frame cache ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_frame_cache_dedupes_calls_within_ttl():
    ctx = _make_ctx(ttl=10.0)
    src = _StubFrameSource([b"frame-a", b"frame-b"])
    ctx.register_frame_source("front-porch", src)

    first = await ctx.get_frame("front-porch")
    second = await ctx.get_frame("front-porch")
    assert first == b"frame-a"
    assert second == b"frame-a"  # cached
    assert src.calls == 1


@pytest.mark.asyncio
async def test_frame_cache_invalidates_after_ttl():
    ctx = _make_ctx(ttl=0.0)  # immediately stale
    src = _StubFrameSource([b"frame-a", b"frame-b"])
    ctx.register_frame_source("front-porch", src)

    first = await ctx.get_frame("front-porch")
    second = await ctx.get_frame("front-porch")
    assert first == b"frame-a"
    assert second == b"frame-b"
    assert src.calls == 2


@pytest.mark.asyncio
async def test_frame_cache_unknown_camera_raises_lookup_error():
    ctx = _make_ctx()
    with pytest.raises(LookupError, match="not configured"):
        await ctx.get_frame("unknown-camera")


@pytest.mark.asyncio
async def test_frame_cache_unregistered_source_raises():
    ctx = _make_ctx()
    # camera is in config but no source registered
    with pytest.raises(LookupError, match="no frame source registered"):
        await ctx.get_frame("front-porch")


@pytest.mark.asyncio
async def test_frame_cache_propagates_frame_source_errors():
    ctx = _make_ctx()

    class _Boom:
        camera_id = "front-porch"

        def fetch(self):
            raise FrameSourceError("camera offline")

    ctx.register_frame_source("front-porch", _Boom())
    with pytest.raises(FrameSourceError, match="camera offline"):
        await ctx.get_frame("front-porch")


@pytest.mark.asyncio
async def test_frame_cache_invalidate_all():
    ctx = _make_ctx(ttl=60.0)
    src = _StubFrameSource([b"a", b"b"])
    ctx.register_frame_source("front-porch", src)
    await ctx.get_frame("front-porch")
    ctx.invalidate_frame_cache()
    await ctx.get_frame("front-porch")
    assert src.calls == 2


@pytest.mark.asyncio
async def test_frame_cache_concurrent_fetches_dedupe():
    """Two coroutines racing for the same camera must produce only
    one upstream fetch — the lock serialises them."""
    ctx = _make_ctx(ttl=60.0)
    src = _StubFrameSource([b"a", b"b"])
    ctx.register_frame_source("front-porch", src)
    a, b = await asyncio.gather(
        ctx.get_frame("front-porch"),
        ctx.get_frame("front-porch"),
    )
    assert a == b == b"a"
    assert src.calls == 1


# ── Event ring ─────────────────────────────────────────────────────


def _event(camera_id: str, seconds_ago: float, summary: str) -> EventRecord:
    return EventRecord(
        received_at=time.time() - seconds_ago,
        camera_id=camera_id,
        adapter="x",
        summary=summary,
    )


def test_event_ring_filters_by_window():
    ctx = _make_ctx()
    ctx.record_event(_event("front-porch", 5, "a"))
    ctx.record_event(_event("front-porch", 100, "b"))
    out = ctx.recent_events(camera_id="front-porch", window_seconds=30)
    assert [e.summary for e in out] == ["a"]


def test_event_ring_returns_newest_first():
    ctx = _make_ctx()
    ctx.record_event(_event("front-porch", 30, "older"))
    ctx.record_event(_event("front-porch", 1, "newer"))
    out = ctx.recent_events(camera_id="front-porch", window_seconds=60)
    assert [e.summary for e in out] == ["newer", "older"]


def test_event_ring_filter_none_means_all_cameras():
    ctx = _make_ctx()
    ctx.record_event(_event("front-porch", 1, "porch"))
    ctx.record_event(_event("back-door", 2, "back"))
    out = ctx.recent_events(camera_id=None, window_seconds=60)
    assert {e.summary for e in out} == {"porch", "back"}


def test_event_ring_filter_specific_camera():
    ctx = _make_ctx()
    ctx.record_event(_event("front-porch", 1, "porch"))
    ctx.record_event(_event("back-door", 1, "back"))
    out = ctx.recent_events(camera_id="front-porch", window_seconds=60)
    assert [e.summary for e in out] == ["porch"]


def test_event_ring_bounded():
    ctx = _make_ctx(ring_size=3)
    for i in range(10):
        ctx.record_event(_event("front-porch", 0, f"e{i}"))
    out = ctx.recent_events(camera_id="front-porch", window_seconds=60)
    # The deque keeps only the last 3 inserted; ordering is newest-first.
    assert [e.summary for e in out] == ["e9", "e8", "e7"]


def test_event_ring_unknown_camera_returns_empty():
    ctx = _make_ctx()
    assert ctx.recent_events(camera_id="nope", window_seconds=60) == []


# ── NATS event parsing ─────────────────────────────────────────────


def test_adapter_from_subject():
    assert _adapter_from_subject("opennvr.inference.yolov8.cam-1.completed") == "yolov8"
    assert _adapter_from_subject("garbage") == "unknown"


def test_summarise_face_recognition_recognised():
    payload = {
        "result": {
            "task": "face_recognition",
            "recognized": True,
            "name": "Alice",
            "similarity": 0.87,
        }
    }
    assert "Alice" in _summarise_event(payload)
    assert "0.87" in _summarise_event(payload)


def test_summarise_face_recognition_unknown():
    payload = {"result": {"task": "face_recognition", "recognized": False}}
    assert "not recognised" in _summarise_event(payload)


def test_summarise_object_detection():
    payload = {
        "result": {
            "task": "object_detection",
            "detections": [
                {"label": "person"}, {"label": "car"}, {"label": "person"},
            ],
        }
    }
    summary = _summarise_event(payload)
    assert "car" in summary and "person" in summary


def test_parse_inference_event_requires_camera_id():
    assert _parse_inference_event("opennvr.inference.x.y.completed", {}) is None
    record = _parse_inference_event(
        "opennvr.inference.yolov8.front.completed",
        {"camera_id": "front", "result": {"task": "face_detection", "face_count": 2}},
    )
    assert record is not None
    assert record.camera_id == "front"
    assert "2 face" in record.summary


def test_parse_inference_event_pulls_camera_from_source_block():
    record = _parse_inference_event(
        "opennvr.inference.yolov8.front.completed",
        {"source": {"camera_id": "front"}, "result": {"task": "scene_caption", "caption": "a box"}},
    )
    assert record is not None
    assert record.camera_id == "front"
    assert "scene" in record.summary


# ── Tier-0's newest frame ahead of our own grab ───────────────────────

@pytest.mark.asyncio
async def test_get_frame_prefers_the_pipelines_latest_frame():
    """Measured on a Ventuno Q: the agent's own RTSP grab took 5.6–6.2 s a
    look while the pipeline had the same scene decoded already."""
    ctx = _make_ctx(ttl=0.0)
    src = _StubFrameSource([b"rtsp-a", b"rtsp-b"])
    ctx.register_frame_source("front-porch", src)
    asked = []

    async def latest(camera_id):
        asked.append(camera_id)
        return b"tier0-frame"
    ctx.set_latest_frame_fetch(latest)
    assert await ctx.get_frame("front-porch") == b"tier0-frame"
    assert asked == ["front-porch"] and src.calls == 0
    assert ctx.review_timestamps("front-porch")          # fed the review ring like a real fetch


@pytest.mark.asyncio
async def test_get_frame_falls_back_to_the_source_when_the_pipeline_has_nothing():
    ctx = _make_ctx(ttl=0.0)
    src = _StubFrameSource([b"rtsp-a"])
    ctx.register_frame_source("front-porch", src)

    async def nothing(camera_id):
        return None
    ctx.set_latest_frame_fetch(nothing)
    assert await ctx.get_frame("front-porch") == b"rtsp-a" and src.calls == 1

    async def broken(camera_id):
        raise RuntimeError("pipeline down")
    ctx2 = _make_ctx(ttl=0.0)
    ctx2.register_frame_source("front-porch", _StubFrameSource([b"rtsp-x"]))
    ctx2.set_latest_frame_fetch(broken)
    assert await ctx2.get_frame("front-porch") == b"rtsp-x"

