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
import time
import types
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
async def test_describe_uses_best_frame_when_track_active():
    ctx = _ctx_with_camera()
    stub = _StubFrameSource()
    ctx.register_frame_source("front-porch", stub)
    # A Tier-0 track is active RIGHT NOW, so the best frame is a valid stand-in
    # for the live scene and describe_camera uses it (the efficiency win). When
    # no track is active the frame is stale and a live grab is taken instead
    # (see tests/test_live_describe_freshness.py).
    ctx.latest_inference = lambda cam, adapter=None: types.SimpleNamespace(
        received_at=time.time(), raw={})
    fetch = AsyncMock(return_value=b"BESTFRAME")
    tools = _build_tools(ctx, best_frame_fetch=fetch)
    await tools.describe_camera({"camera_id": "front-porch", "question": "what colour?"})
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


# ── search_history (canonical event store, RFC-0001 C1) ─────────────

class _FakeEvent:
    def __init__(self, id, camera_id=3, label="person", has_evidence=True,
                 plate_text=None,
                 started_at="2026-08-12T15:12:04+00:00",
                 ended_at="2026-08-12T15:14:11+00:00"):
        self.id = id
        self.camera_id = camera_id
        self.label = label
        self.score = 0.9
        self.started_at = started_at
        self.ended_at = ended_at
        self.stationary = False
        self.plate_text = plate_text
        self.has_evidence = has_evidence


class _FakeEventsClient:
    def __init__(self, events, crops=None):
        self._events = events
        self._crops = crops or {}
        self.searches = []

    async def search(self, **kw):
        self.searches.append(kw)
        return self._events

    async def evidence(self, event_id):
        return self._crops.get(event_id)


class _FakeRecogniser:
    def __init__(self, by_crop):
        self._by = by_crop

    async def infer(self, frame_jpeg=None, extra=None):
        name = self._by.get(frame_jpeg)
        if name:
            return {"result": {"recognized": True, "name": name}}
        return {"result": {}}


def _history_tools(events_client, recogniser=None):
    from unittest.mock import AsyncMock
    stub = AsyncMock()
    stub.infer.return_value = {"result": {}}
    return CameraTools(
        context=_ctx_with_camera(),
        caption_client=stub,
        detection_client=stub,
        recognition_client=recogniser or stub,
        events_client=events_client,
    )


def test_search_history_reports_visits_and_names(anyio_backend=None):
    import asyncio
    ec = _FakeEventsClient(
        [_FakeEvent(1), _FakeEvent(2, has_evidence=False)],
        crops={1: b"crop-1"},
    )
    tools = _history_tools(ec, _FakeRecogniser({b"crop-1": "Priya"}))
    out = asyncio.run(tools.search_history({
        "label": "person",
        "start_time": "2026-08-12T15:00", "end_time": "2026-08-12T16:00",
    }))
    assert "2 person visit(s)" in out
    assert "photo kept" in out          # times are spoken in LOCAL tz (host-dependent)
    assert "Recognised: Priya" in out
    assert ec.searches[0]["label"] == "person"


def test_search_history_without_store_or_matches(anyio_backend=None):
    import asyncio
    tools = _history_tools(None)
    assert "isn't enabled" in asyncio.run(tools.search_history({}))
    ec = _FakeEventsClient([])
    tools2 = _history_tools(ec)
    out = asyncio.run(tools2.search_history({"label": "car"}))
    assert "No car visits" in out


def test_search_history_failure_is_not_reported_as_empty(anyio_backend=None):
    # Store down / rejected query must NOT sound like "nobody came".
    import asyncio

    class _DownClient:
        async def search(self, **kw):
            return None

    tools = _history_tools(_DownClient())
    out = asyncio.run(tools.search_history({"label": "person"}))
    assert "couldn't check" in out
    assert "No person visits" not in out


def test_search_history_speaks_plates(anyio_backend=None):
    import asyncio
    ec = _FakeEventsClient([_FakeEvent(1, label="car", plate_text="KA01AB1234")])
    tools = _history_tools(ec)
    out = asyncio.run(tools.search_history({"label": "car", "plate": "KA01"}))
    assert "plate KA01AB1234" in out
    assert ec.searches[0]["plate"] == "KA01"


# ── remembered photos reach the chat ────────────────────────────────


def test_search_history_publishes_the_remembered_photos(anyio_backend=None):
    """The answer already said "(photo kept)" and the crop was already being
    fetched to face-match — then thrown away, so an answer about 16:25 arrived
    as bare text. Put the photo on the turn so the UI can show it."""
    import asyncio, base64
    ec = _FakeEventsClient(
        [_FakeEvent(7), _FakeEvent(8), _FakeEvent(9, has_evidence=False)],
        crops={7: b"crop-7", 8: b"crop-8"},
    )
    tools = _history_tools(ec)
    asyncio.run(tools.search_history({"label": "person"}))

    frames = tools.last_evidence_frames
    assert len(frames) == 2, "both visits with a kept photo should be published"
    assert [base64.b64decode(f["jpeg_b64"]) for f in frames] == [b"crop-7", b"crop-8"]
    # A visit with no evidence contributes nothing rather than a broken image.
    assert all(f["jpeg_b64"] for f in frames)


def test_published_photos_are_captioned_with_their_time(anyio_backend=None):
    """Every frame the chat has ever rendered was the LIVE view. An
    uncaptioned crop from this afternoon sitting in an answer would read as
    "here is your camera now" — the wrong thing to get wrong in a security
    product. Each historical frame carries its own identity and time."""
    import asyncio
    ec = _FakeEventsClient([_FakeEvent(42)], crops={42: b"crop"})
    tools = _history_tools(ec)
    asyncio.run(tools.search_history({"label": "person"}))
    cap = tools.last_evidence_frames[0]["caption"]
    assert "#42" in cap, f"caption must identify the visit, got {cap!r}"
    assert any(ch.isdigit() for ch in cap.split("·")[-1]), (
        f"caption must carry a time, got {cap!r}")


def test_publishing_photos_is_capped(anyio_backend=None):
    """A busy afternoon must not dump twenty images into the chat."""
    import asyncio
    events = [_FakeEvent(i) for i in range(1, 11)]
    ec = _FakeEventsClient(events, crops={i: b"c%d" % i for i in range(1, 11)})
    tools = _history_tools(ec)
    asyncio.run(tools.search_history({"label": "person"}))
    assert len(tools.last_evidence_frames) == 3


def test_a_failed_evidence_fetch_does_not_break_the_answer(anyio_backend=None):
    """The text answer is the thing that must survive; the photo is a bonus."""
    import asyncio

    class _Broken(_FakeEventsClient):
        async def evidence(self, event_id):
            raise RuntimeError("store went away")

    ec = _Broken([_FakeEvent(1)], crops={})
    tools = _history_tools(ec)
    out = asyncio.run(tools.search_history({"label": "person"}))
    assert "1 person visit(s)" in out
    assert tools.last_evidence_frames == []


# ── history + the visit still being written ─────────────────────────


class _RingEv:
    def __init__(self, tracks, camera_id="cam1", age=2.0):
        import time
        self.received_at = time.time() - age
        self.adapter = "tier0"
        self.camera_id = camera_id
        self.raw = {"tracks": tracks}


def test_history_admits_a_visit_still_in_progress(anyio_backend=None):
    """A visit enters the store only when it ENDS. Field report: the operator
    walked in to test, asked 'did any person come today', and was denied while
    standing on camera — the visit was still being written. The live Tier-0
    ring knows; the answer must say so."""
    import asyncio
    tools = _history_tools(_FakeEventsClient([]))
    tools._ctx.recent_events = (
        lambda *, camera_id, window_seconds: [_RingEv([{"label": "person"}])])
    out = asyncio.run(tools.search_history({"label": "person"}))
    assert "No person visits remembered" in out
    assert "right now" in out and "still in progress" in out, out


def test_no_in_progress_note_when_the_ring_is_quiet(anyio_backend=None):
    import asyncio
    tools = _history_tools(_FakeEventsClient([]))
    tools._ctx.recent_events = lambda *, camera_id, window_seconds: []
    out = asyncio.run(tools.search_history({"label": "person"}))
    assert "right now" not in out


def test_in_progress_note_matches_the_asked_label(anyio_backend=None):
    """A cat on camera is not evidence about a person question."""
    import asyncio
    tools = _history_tools(_FakeEventsClient([]))
    tools._ctx.recent_events = (
        lambda *, camera_id, window_seconds: [_RingEv([{"label": "cat"}])])
    out = asyncio.run(tools.search_history({"label": "person"}))
    assert "right now" not in out


def test_a_broken_ring_never_breaks_the_history_answer(anyio_backend=None):
    import asyncio
    tools = _history_tools(_FakeEventsClient([_FakeEvent(1)], crops={1: b"c"}))

    def boom(*, camera_id, window_seconds):
        raise RuntimeError("ring exploded")

    tools._ctx.recent_events = boom
    out = asyncio.run(tools.search_history({"label": "person"}))
    assert "1 person visit(s)" in out
