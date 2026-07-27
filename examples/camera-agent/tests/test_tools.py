# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the LLM tool handlers — describe_camera, detect_objects,
recognize_faces, recent_events. The KAI-C adapter clients are mocked
so no HTTP fires; each test pins one tool's response-shape handling.

Tool result strings are designed to flow into the LLM as plain prose,
so the tests assert on substrings rather than exact equality —
phrasing is allowed to evolve."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from context import CameraContext, CameraSpec, EventRecord
from tools import CameraTools, build_tool_definitions


class _StubFrameSource:
    camera_id = "front-porch"

    def __init__(self, frame: bytes = b"\xff\xd8jpeg") -> None:
        self._frame = frame
        self.calls = 0

    def fetch(self) -> bytes:
        self.calls += 1
        return self._frame


def _ctx_with_camera() -> CameraContext:
    spec = CameraSpec(
        camera_id="front-porch",
        frame_url="http://x",
        role="entrance",
    )
    ctx = CameraContext(cameras=[spec], frame_cache_ttl_seconds=5.0)
    ctx.register_frame_source("front-porch", _StubFrameSource())
    return ctx


def _build_tools(ctx: CameraContext, *,
                 caption_response=None,
                 detection_response=None,
                 recognition_response=None,
                 best_frame_fetch=None,
                 resolve_camera=None) -> CameraTools:
    caption = AsyncMock()
    caption.infer.return_value = caption_response or {
        "result": {"caption": "a box on a doormat"}
    }
    detect = AsyncMock()
    detect.infer.return_value = detection_response or {
        "result": {"detections": [{"label": "person"}]}
    }
    recognise = AsyncMock()
    recognise.infer.return_value = recognition_response or {
        "result": {"recognized": False, "face_bbox": [10, 10, 50, 50]}
    }
    return CameraTools(
        context=ctx,
        caption_client=caption,
        detection_client=detect,
        recognition_client=recognise,
        best_frame_fetch=best_frame_fetch,
        resolve_camera=resolve_camera,
    )


# ── describe_camera ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_describe_camera_returns_caption():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx)
    result = await tools.describe_camera({"camera_id": "front-porch"})
    assert "box on a doormat" in result
    assert "front-porch" in result


@pytest.mark.asyncio
async def test_describe_camera_unknown_id_returns_error_string():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx)
    result = await tools.describe_camera({"camera_id": "kitchen"})
    assert result.startswith("ERROR:")
    assert "kitchen" in result


@pytest.mark.asyncio
async def test_describe_camera_empty_caption_uses_fallback():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx, caption_response={"result": {"caption": ""}})
    result = await tools.describe_camera({"camera_id": "front-porch"})
    # No caption available -> fall back to object detection so the user
    # still gets a grounded answer rather than an error or a made-up scene.
    assert "person" in result
    assert "front-porch" in result


@pytest.mark.asyncio
async def test_describe_camera_caption_exception_uses_detection_fallback():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx)
    tools._caption.infer.side_effect = RuntimeError("502 from BLIP")
    result = await tools.describe_camera({"camera_id": "front-porch"})
    # Caption adapter erroring -> grounded detection fallback, not a raw error.
    assert "person" in result
    assert "front-porch" in result


# ── detect_objects ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_objects_groups_duplicate_labels():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx, detection_response={
        "result": {"detections": [
            {"label": "person"},
            {"label": "person"},
            {"label": "car"},
        ]},
    })
    result = await tools.detect_objects({"camera_id": "front-porch"})
    # Counts are pluralised naturally for speech ("2 people", not "2x person").
    assert "2 people" in result
    assert "car" in result


@pytest.mark.asyncio
async def test_detect_objects_no_detections():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx, detection_response={"result": {"detections": []}})
    result = await tools.detect_objects({"camera_id": "front-porch"})
    assert "no objects" in result.lower()


@pytest.mark.asyncio
async def test_detect_objects_caps_to_eight_labels():
    """A scene with many unique labels must not flood the LLM context.
    Cap to 8 labels in the summary."""
    ctx = _ctx_with_camera()
    detections = [{"label": f"thing{i}"} for i in range(20)]
    tools = _build_tools(ctx, detection_response={"result": {"detections": detections}})
    result = await tools.detect_objects({"camera_id": "front-porch"})
    # Count comma-separated entries; should be ≤ 8.
    body = result.split(":", 1)[1] if ":" in result else result
    parts = [p.strip() for p in body.rstrip(".").split(",")]
    assert len(parts) <= 8


# ── recognize_faces ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recognize_faces_known_person():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx, recognition_response={
        "result": {
            "recognized": True,
            "name": "Alice",
            "category": "family",
            "similarity": 0.88,
        },
    })
    result = await tools.recognize_faces({"camera_id": "front-porch"})
    assert "Alice" in result
    assert "family" in result
    assert "0.88" in result


@pytest.mark.asyncio
async def test_recognize_faces_unknown_face():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx, recognition_response={
        "result": {"recognized": False, "face_bbox": [1, 2, 3, 4]},
    })
    result = await tools.recognize_faces({"camera_id": "front-porch"})
    assert "not registered" in result


@pytest.mark.asyncio
async def test_recognize_faces_no_face():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx, recognition_response={
        "result": {"recognized": False, "face_bbox": None},
    })
    result = await tools.recognize_faces({"camera_id": "front-porch"})
    assert "no face" in result.lower()


# ── recent_events ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_events_no_events():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx)
    result = await tools.recent_events({
        "camera_id": "front-porch", "window_seconds": 60,
    })
    assert "No events" in result


@pytest.mark.asyncio
async def test_recent_events_returns_summary_lines():
    ctx = _ctx_with_camera()
    ctx.record_event(EventRecord(
        received_at=time.time() - 5,
        camera_id="front-porch",
        adapter="yolov8",
        summary="person detected",
    ))
    tools = _build_tools(ctx)
    result = await tools.recent_events({
        "camera_id": "front-porch", "window_seconds": 60,
    })
    assert "person detected" in result
    assert "5s ago" in result


@pytest.mark.asyncio
async def test_recent_events_any_camera_wildcard():
    ctx = _ctx_with_camera()
    ctx.record_event(EventRecord(
        received_at=time.time(),
        camera_id="front-porch",
        adapter="x",
        summary="alpha",
    ))
    tools = _build_tools(ctx)
    result = await tools.recent_events({
        "camera_id": "__any__", "window_seconds": 60,
    })
    assert "alpha" in result


@pytest.mark.asyncio
async def test_recent_events_rejects_negative_window():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx)
    result = await tools.recent_events({
        "camera_id": "front-porch", "window_seconds": -5,
    })
    assert result.startswith("ERROR")


@pytest.mark.asyncio
async def test_recent_events_rejects_unknown_camera():
    ctx = _ctx_with_camera()
    tools = _build_tools(ctx)
    result = await tools.recent_events({
        "camera_id": "kitchen", "window_seconds": 60,
    })
    assert result.startswith("ERROR")


# ── Tool definitions ───────────────────────────────────────────────


def test_tool_definitions_bake_camera_ids_into_enum():
    """The LLM should not be able to invent camera names — the enum
    constrains it to configured cameras at the protocol level."""
    defs = build_tool_definitions(["front-porch", "back-door"])
    describe = next(d for d in defs if d["function"]["name"] == "describe_camera")
    enum = describe["function"]["parameters"]["properties"]["camera_id"]["enum"]
    # Configured cameras plus the "all" selector — still can't invent names.
    assert set(enum) == {"front-porch", "back-door", "all"}


def test_tool_definitions_recent_events_offers_any_wildcard():
    defs = build_tool_definitions(["front-porch"])
    recent = next(d for d in defs if d["function"]["name"] == "recent_events")
    enum = recent["function"]["parameters"]["properties"]["camera_id"]["enum"]
    assert "__any__" in enum
    assert "front-porch" in enum


def test_tool_definitions_with_no_cameras_has_sentinel():
    """An empty camera list shouldn't crash schema generation —
    insert a sentinel value so the LLM sees a usable enum and the
    handler can return ERROR cleanly."""
    defs = build_tool_definitions([])
    describe = next(d for d in defs if d["function"]["name"] == "describe_camera")
    enum = describe["function"]["parameters"]["properties"]["camera_id"]["enum"]
    assert enum  # non-empty


# ── camera_snapshot: metadata from Tier-0, no inference ────────────

def _record_tier0(ctx: CameraContext, camera_id: str, labels: list[str]) -> None:
    ctx.record_event(EventRecord(
        received_at=time.time(), camera_id=camera_id, adapter="tier0",
        summary="tier0", raw={"tracks": [{"label": lbl} for lbl in labels]},
    ))


@pytest.mark.asyncio
async def test_camera_snapshot_counts_from_tier0_without_inference():
    ctx = _ctx_with_camera()
    _record_tier0(ctx, "front-porch", ["person", "car", "car"])
    tools = _build_tools(ctx)
    out = await tools.camera_snapshot({"camera_id": "front-porch"})
    assert "a person" in out and "2 cars" in out
    # no live inference fired — the whole point of the tool
    tools._detect.infer.assert_not_awaited()
    tools._caption.infer.assert_not_awaited()


@pytest.mark.asyncio
async def test_camera_snapshot_reports_when_no_tier0_data():
    tools = _build_tools(_ctx_with_camera())
    out = await tools.camera_snapshot({"camera_id": "front-porch"})
    assert "no live detection data" in out


@pytest.mark.asyncio
async def test_camera_snapshot_resolves_agent_camera_to_pipeline_id():
    # The Tier-0 ring is keyed by the pipeline camera id ("3"); the tool is called
    # with the agent id ("front-porch"). Without the resolver they'd never match.
    ctx = _ctx_with_camera()
    _record_tier0(ctx, "3", ["person"])                 # ring keyed by pipeline id
    tools = _build_tools(ctx, resolve_camera=lambda c: "3")
    out = await tools.camera_snapshot({"camera_id": "front-porch"})
    assert "a person" in out


@pytest.mark.asyncio
async def test_camera_snapshot_ignores_stale_events():
    ctx = _ctx_with_camera()
    ctx.record_event(EventRecord(
        received_at=time.time() - 30, camera_id="front-porch", adapter="tier0",
        summary="old", raw={"tracks": [{"label": "person"}]},
    ))
    out = await _build_tools(ctx).camera_snapshot({"camera_id": "front-porch"})
    assert "nothing detected recently" in out


@pytest.mark.asyncio
async def test_describe_swallows_best_frame_fetch_errors():
    ctx = _ctx_with_camera()
    stub = _StubFrameSource(frame=b"LIVEFRAME")
    ctx.register_frame_source("front-porch", stub)
    boom = AsyncMock(side_effect=RuntimeError("network down"))
    tools = _build_tools(ctx, best_frame_fetch=boom)
    await tools.describe_camera({"camera_id": "front-porch"})   # must not raise
    assert tools._caption.infer.await_args.kwargs["frame_jpeg"] == b"LIVEFRAME"
    assert stub.calls == 1


# ── describe_camera prefers Tier-0's best frame ────────────────────

@pytest.mark.asyncio
async def test_describe_uses_best_frame_when_available():
    ctx = _ctx_with_camera()
    stub = _StubFrameSource()
    ctx.register_frame_source("front-porch", stub)
    fetch = AsyncMock(return_value=b"BESTFRAME")
    tools = _build_tools(ctx, best_frame_fetch=fetch)
    await tools.describe_camera({"camera_id": "front-porch", "question": "what colour?"})
    # the VLM ran on the best frame, and no live grab happened
    assert tools._caption.infer.await_args.kwargs["frame_jpeg"] == b"BESTFRAME"
    assert stub.calls == 0


@pytest.mark.asyncio
async def test_describe_falls_back_to_live_frame_when_no_best():
    ctx = _ctx_with_camera()
    stub = _StubFrameSource(frame=b"LIVEFRAME")
    ctx.register_frame_source("front-porch", stub)
    fetch = AsyncMock(return_value=None)          # best frame unavailable
    tools = _build_tools(ctx, best_frame_fetch=fetch)
    await tools.describe_camera({"camera_id": "front-porch"})
    assert tools._caption.infer.await_args.kwargs["frame_jpeg"] == b"LIVEFRAME"
    assert stub.calls == 1


# ── make_best_frame_fetch client ───────────────────────────────────

@pytest.mark.asyncio
async def test_make_best_frame_fetch_maps_camera_and_handles_status():
    from tools import make_best_frame_fetch
    seen = {}

    async def http_get(url):
        seen["url"] = url
        return (200, b"IMG") if "camera=7" in url else (404, b"")

    fetch = make_best_frame_fetch(
        "http://tier0:9109/", resolve_camera=lambda c: "7", http_get=http_get)
    assert await fetch("front-porch") == b"IMG"
    assert seen["url"] == "http://tier0:9109/best_frame?camera=7"

    fetch_miss = make_best_frame_fetch(
        "http://tier0:9109", resolve_camera=lambda c: "9", http_get=http_get)
    assert await fetch_miss("x") is None          # 404 -> None
