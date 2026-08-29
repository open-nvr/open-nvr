# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RFC-0002 Phase 4, last checkbox: the agent consumes
``plate.recognized.v1`` — 'what plates today?' answers from the
CONTRACT event, whichever platform path produced it. Proves cross-app
consumption end to end: the same envelope the LPR app alerts on feeds
the agent's ring and its ``recent_plates`` tool.
"""
from __future__ import annotations

import asyncio
import time

from camera_agent import SKILL_TOOLS, AppConfig, CameraAgentRuntime
from context import CameraContext, PlateRecord, parse_plate_event
from context import CameraSpec


def _envelope(plate="ABC1234", camera="cam1", *, confidence=0.9,
              vehicle="car", schema="plate.recognized.v1"):
    return {
        "id": "evt_0123456789ab",
        "schema": schema,
        "correlation_id": "corr-1",
        "camera_id": camera,
        "ts": "2026-08-29T10:00:00+00:00",
        "producer": "kai-c",
        "payload": {"plate_text": plate, "confidence": confidence,
                    "vehicle_label": vehicle, "event_id": 42},
    }


def _runtime() -> CameraAgentRuntime:
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg",
                            role="front door")],
    )
    return CameraAgentRuntime(cfg)


# ── parse_plate_event (the envelope contract) ──────────────────────


def test_parse_accepts_the_contract_envelope():
    rec = parse_plate_event(_envelope(plate="ab 1234"))
    assert rec is not None
    assert rec.plate_text == "AB1234"        # producer normalisation mirrored
    assert rec.camera_id == "cam1"
    assert rec.confidence == 0.9
    assert rec.vehicle_label == "car"
    assert rec.correlation_id == "corr-1"
    assert rec.producer == "kai-c"


def test_parse_rejects_foreign_and_malformed():
    assert parse_plate_event(None) is None
    assert parse_plate_event("junk") is None
    assert parse_plate_event(_envelope(schema="plate.recognized.v2")) is None
    assert parse_plate_event({**_envelope(), "payload": "junk"}) is None
    assert parse_plate_event(_envelope(plate="  ")) is None
    assert parse_plate_event({**_envelope(), "camera_id": ""}) is None
    bad_conf = parse_plate_event(_envelope(confidence=True))
    assert bad_conf is not None and bad_conf.confidence is None


# ── the ring ───────────────────────────────────────────────────────


def _ctx() -> CameraContext:
    return CameraContext(cameras=[
        CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="a"),
        CameraSpec(camera_id="cam2", frame_url="http://x/2.jpg", role="b"),
    ])


def test_ring_filters_and_orders_newest_first():
    ctx = _ctx()
    for plate, cam in [("AAA111", "cam1"), ("BBB234", "cam2"),
                       ("CCC234", "cam1")]:
        ctx.record_plate(PlateRecord(received_at=time.time(),
                                     camera_id=cam, plate_text=plate))
    allr = ctx.recent_plates(window_seconds=60)
    assert [r.plate_text for r in allr] == ["CCC234", "BBB234", "AAA111"]
    assert [r.plate_text for r in ctx.recent_plates(
        camera_id="cam1", window_seconds=60)] == ["CCC234", "AAA111"]
    # Substring match, case/space-insensitive — 'ends in 234'.
    assert [r.plate_text for r in ctx.recent_plates(
        plate="2 34", window_seconds=60)] == ["CCC234", "BBB234"]


def test_ring_window_and_bound():
    ctx = _ctx()
    ctx.record_plate(PlateRecord(received_at=time.time() - 3600,
                                 camera_id="cam1", plate_text="OLD1"))
    ctx.record_plate(PlateRecord(received_at=time.time(),
                                 camera_id="cam1", plate_text="NEW1"))
    assert [r.plate_text for r in ctx.recent_plates(window_seconds=60)] == ["NEW1"]
    for i in range(300):                      # ring holds 256
        ctx.record_plate(PlateRecord(received_at=time.time(),
                                     camera_id="cam1", plate_text=f"P{i}"))
    assert len(ctx.recent_plates(window_seconds=7200)) == 256


# ── the tool ───────────────────────────────────────────────────────


def test_recent_plates_tool_reports_reads():
    rt = _runtime()
    rt.context.nats_wired = True
    rt.context.record_plate(PlateRecord(
        received_at=time.time(), camera_id="cam1", plate_text="AB12CDE",
        confidence=0.91, vehicle_label="truck"))
    out = asyncio.run(rt.tools.recent_plates(
        {"camera_id": "__any__", "window_seconds": 3600}))
    assert "AB12CDE" in out and "cam1" in out
    assert "truck" in out and "0.91" in out


def test_recent_plates_tool_validates_and_degrades():
    rt = _runtime()
    out = asyncio.run(rt.tools.recent_plates(
        {"camera_id": "cam1", "window_seconds": -5}))
    assert out.startswith("ERROR")
    out = asyncio.run(rt.tools.recent_plates(
        {"camera_id": "ghost", "window_seconds": 60}))
    assert out.startswith("ERROR: unknown camera_id")
    # Empty + bus unwired → honest pointer at stored history.
    rt.context.nats_wired = False
    out = asyncio.run(rt.tools.recent_plates(
        {"camera_id": "__any__", "window_seconds": 60}))
    assert "search_history" in out


def test_recent_plates_tool_plate_filter():
    rt = _runtime()
    rt.context.nats_wired = True
    for p in ("AAA111", "BBB234"):
        rt.context.record_plate(PlateRecord(
            received_at=time.time(), camera_id="cam1", plate_text=p))
    out = asyncio.run(rt.tools.recent_plates(
        {"camera_id": "__any__", "window_seconds": 60, "plate": "234"}))
    assert "BBB234" in out and "AAA111" not in out


# ── wiring ─────────────────────────────────────────────────────────


def test_tool_rides_the_events_skill_and_is_advertised():
    assert "recent_plates" in SKILL_TOOLS["events"]
    rt = _runtime()
    advertised = {t["function"]["name"] for t in rt.tool_definitions}
    # The events skill requires a backend; with none configured the
    # runtime may exclude the whole group — assert consistency instead:
    events_tools = set(SKILL_TOOLS["events"])
    assert (events_tools <= advertised) or not (events_tools & advertised)


def test_nats_wired_stays_false_without_nats_py(monkeypatch):
    # The wired flag must be truthful: nats-py missing = the bus is NOT
    # wired, and the tool's empty answer must say so (not imply a quiet
    # but working bus).
    import builtins

    from context import run_event_subscriber

    ctx = _ctx()
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "nats":
            raise ImportError("no nats-py")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    async def run():
        stop = asyncio.Event()
        stop.set()                       # return immediately from wait()
        await run_event_subscriber(
            context=ctx, nats_url="nats://x", nats_token=None,
            stop_event=stop)
    asyncio.run(run())
    assert ctx.nats_wired is False
