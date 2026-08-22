# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Multi-camera tool resolution/aggregation + the standing-monitor engine."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


def _runtime(detections=None):
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[
            CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front"),
            CameraSpec(camera_id="cam2", frame_url="http://x/2.jpg", role="gate"),
            CameraSpec(camera_id="cam3", frame_url="http://x/3.jpg", role="yard"),
        ],
    )
    rt = CameraAgentRuntime(cfg)

    async def fake_get_frame(cam, **_kw):
        return b"\xff\xd8\xff" + cam.encode()

    async def fake_infer(*, frame_jpeg, **kw):
        return {"result": {"detections": detections if detections is not None else [{"label": "person"}]}}

    rt.context.get_frame = fake_get_frame
    rt.detection_client.infer = fake_infer
    return rt


# ── multi-camera tool resolution ───────────────────────────────────────


def test_resolve_all_returns_every_camera():
    rt = _runtime()
    cams = rt.tools._resolve_cameras({"camera_id": "all"})
    assert cams == ["cam1", "cam2", "cam3"]
    assert rt.tools.last_cameras_used == ["cam1", "cam2", "cam3"]


def test_resolve_list_dedupes_and_validates():
    rt = _runtime()
    assert rt.tools._resolve_cameras({"camera_ids": ["cam2", "cam2", "cam1"]}) == ["cam2", "cam1"]
    assert rt.tools._resolve_cameras({"camera_ids": ["cam9"]}).startswith("ERROR:")


def test_detect_objects_single_camera_phrasing():
    rt = _runtime(detections=[{"label": "person"}])
    out = asyncio.run(rt.tools.detect_objects({"camera_id": "cam1"}))
    assert out.startswith("On cam1:")


def test_detect_objects_all_cameras_aggregates():
    rt = _runtime(detections=[{"label": "person"}, {"label": "car"}])
    out = asyncio.run(rt.tools.detect_objects({"camera_id": "all"}))
    assert "Across 3 cameras" in out
    assert "cam1:" in out and "cam2:" in out and "cam3:" in out


# ── monitor engine ─────────────────────────────────────────────────────


def test_notify_monitor_raises_notification_when_target_present():
    rt = _runtime(detections=[{"label": "person"}])

    async def go():
        rt.monitors._cooldown = 0.0
        mon = rt.monitors.create(kind="notify", camera_ids=["cam1"], target="person",
                                 interval_s=0.05)
        for _ in range(40):
            if rt.monitors.notifications():
                break
            await asyncio.sleep(0.02)
        notes = rt.monitors.notifications()
        rt.monitors.stop(mon.id)
        assert notes and "person" in notes[0]["text"] and "cam1" in notes[0]["text"]

    asyncio.run(go())


def test_count_monitor_tracks_current_and_peak():
    rt = _runtime(detections=[{"label": "person"}, {"label": "person"}])

    async def go():
        mon = rt.monitors.create(kind="count", camera_ids=["cam2"], target="person",
                                 interval_s=0.05)
        for _ in range(40):
            if rt.monitors.list()[0]["current"].get("cam2"):
                break
            await asyncio.sleep(0.02)
        m = rt.monitors.list()[0]
        rt.monitors.stop(mon.id)
        assert m["current"]["cam2"] == 2
        assert m["peak"]["cam2"] == 2

    asyncio.run(go())


def test_notify_monitor_silent_when_target_absent():
    rt = _runtime(detections=[{"label": "car"}])  # watching for person

    async def go():
        mon = rt.monitors.create(kind="notify", camera_ids=["cam1"], target="person",
                                 interval_s=0.05)
        await asyncio.sleep(0.2)
        rt.monitors.stop(mon.id)
        assert rt.monitors.notifications() == []

    asyncio.run(go())


def test_create_monitor_handler_and_stop(monkeypatch):
    rt = _runtime()

    async def go():
        msg = await rt._handle_create_monitor(
            {"kind": "notify", "target": "person", "camera_id": "cam1"})
        assert "watch #" in msg
        assert len(rt.monitors.list()) == 1
        mid = rt.monitors.list()[0]["id"]
        stop_msg = await rt._handle_stop_monitor({"monitor_id": mid})
        assert f"#{mid}" in stop_msg

    asyncio.run(go())


# ── endpoints ──────────────────────────────────────────────────────────


def test_monitors_endpoints():
    rt = _runtime()
    client = TestClient(build_app(rt))
    r = client.post("/monitors", json={"kind": "count", "target": "car", "camera_id": "all"})
    assert r.status_code == 202
    body = client.get("/monitors").json()
    assert body["monitors"] and body["monitors"][0]["kind"] == "count"
    mid = body["monitors"][0]["id"]
    assert client.delete(f"/monitors/{mid}").status_code == 200
    # Idempotent delete, same contract as /alarms: already-gone = success.
    gone = client.delete("/monitors/9999")
    assert gone.status_code == 200 and gone.json()["already_gone"] is True
    # invalid create
    assert client.post("/monitors", json={"kind": "bogus", "target": "x", "camera_id": "cam1"}).status_code == 400
