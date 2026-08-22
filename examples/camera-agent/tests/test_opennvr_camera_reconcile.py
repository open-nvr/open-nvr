# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenNVR camera reconcile: the agent must pick up OpenNVR cameras that appear
AFTER startup (the boot-order race the testing team hit) without a restart, and
never tear down working cameras on a transient empty/failed fetch."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec


def _runtime(cameras=None, **cfg_kw):
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="key", system_prompt="t",
        cameras=list(cameras or []), **cfg_kw,
    )
    return CameraAgentRuntime(cfg)


# ── _load_opennvr_cameras parsing ──────────────────────────────────────


def test_load_carries_open_nvr_camera_id(monkeypatch):
    """The internal endpoint returns the server Camera.id as
    open_nvr_camera_id; it must land on the spec for recordings/live."""
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"cameras": [
                {"camera_id": "cam7", "frame_url": "rtsp://mtx/cam7",
                 "name": "Dock", "open_nvr_camera_id": "7"},
                {"camera_id": "cam9", "frame_url": "rtsp://mtx/cam9"},  # no id
            ]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    specs = ca._load_opennvr_cameras(url="http://core/x", api_key="k")
    assert [s.camera_id for s in specs] == ["cam7", "cam9"]
    assert specs[0].opennvr_camera_id == 7
    assert specs[1].opennvr_camera_id is None


def test_load_returns_empty_on_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(httpx, "get", _boom)
    assert ca._load_opennvr_cameras(url="http://core/x", api_key="k") == []


# ── _register_opennvr_cameras (add-only, idempotent, wires sources) ─────


def test_register_adds_wires_source_and_is_idempotent():
    rt = _runtime()
    specs = [CameraSpec(camera_id="cam1", frame_url="rtsp://mtx/cam1", role="front")]
    added = rt._register_opennvr_cameras(specs)
    assert [s.camera_id for s in added] == ["cam1"]
    assert rt.context.known_camera("cam1")
    assert "cam1" in rt.context._frame_sources          # frame source wired
    assert [c.camera_id for c in rt.cfg.cameras] == ["cam1"]
    # second pass with the same camera adds nothing (idempotent)
    assert rt._register_opennvr_cameras(specs) == []
    assert len(rt.cfg.cameras) == 1


def test_register_never_removes_existing_on_empty():
    rt = _runtime(cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")])
    assert rt._register_opennvr_cameras([]) == []
    assert rt.context.known_camera("cam1")               # still there
    assert [c.camera_id for c in rt.cfg.cameras] == ["cam1"]


# ── the reconcile loop self-heals the boot race ────────────────────────


@pytest.mark.asyncio
async def test_reconcile_picks_up_cameras_that_appear_later(monkeypatch):
    """Core has no cameras on the first poll, then registers one — the loop
    must pick it up without a restart."""
    rt = _runtime(opennvr_cameras_url="http://core/cams", opennvr_api_key="k")
    monkeypatch.setattr(ca, "_OPENNVR_CAMERA_RECONCILE_FAST", 0.01)
    monkeypatch.setattr(ca, "_OPENNVR_CAMERA_RECONCILE_SLOW", 0.01)

    polls = {"n": 0}
    late = CameraSpec(camera_id="cam1", frame_url="rtsp://mtx/cam1", role="front")

    def fake_load(*, url, api_key):
        polls["n"] += 1
        return [] if polls["n"] < 2 else [late]

    monkeypatch.setattr(ca, "_load_opennvr_cameras", fake_load)

    task = asyncio.create_task(rt._reconcile_opennvr_cameras())
    # wait (bounded) until the late camera has been registered
    for _ in range(200):
        if rt.context.known_camera("cam1"):
            break
        await asyncio.sleep(0.01)
    rt._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert rt.context.known_camera("cam1")
    assert "cam1" in rt.context._frame_sources
    assert polls["n"] >= 2                                # it retried past the empty poll


@pytest.mark.asyncio
async def test_reconcile_exits_when_url_unset():
    rt = _runtime()   # no opennvr_cameras_url
    # returns immediately, no hang
    await asyncio.wait_for(rt._reconcile_opennvr_cameras(), timeout=1.0)


@pytest.mark.asyncio
async def test_reconcile_survives_a_fetch_exception(monkeypatch):
    rt = _runtime(opennvr_cameras_url="http://core/cams")
    monkeypatch.setattr(ca, "_OPENNVR_CAMERA_RECONCILE_FAST", 0.01)
    monkeypatch.setattr(ca, "_OPENNVR_CAMERA_RECONCILE_SLOW", 0.01)

    calls = {"n": 0}
    good = CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")

    def flaky(*, url, api_key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return [good]

    monkeypatch.setattr(ca, "_load_opennvr_cameras", flaky)
    task = asyncio.create_task(rt._reconcile_opennvr_cameras())
    for _ in range(200):
        if rt.context.known_camera("cam1"):
            break
        await asyncio.sleep(0.01)
    rt._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert rt.context.known_camera("cam1")               # recovered after the error


# ── frame_url refresh (the "no frame after an hour" bug) ───────────────


def test_register_refreshes_frame_url_of_known_camera():
    """OpenNVR bakes a ~60-min MediaMTX JWT into frame_url, so the reconcile
    must swap a known camera's frame source when the served URL changes —
    otherwise previews die within the hour and never recover ("no frame"
    even after a browser refresh, until someone restarts the agent)."""
    rt = _runtime()
    old = CameraSpec(camera_id="cam1",
                     frame_url="http://mtx/cam1/frame.jpg?jwt=OLD", role="front")
    rt._register_opennvr_cameras([old])
    src_before = rt.context._frame_sources["cam1"]

    fresh = CameraSpec(camera_id="cam1",
                       frame_url="http://mtx/cam1/frame.jpg?jwt=FRESH", role="front")
    # still add-only for identity: nothing NEW is reported…
    assert rt._register_opennvr_cameras([fresh]) == []
    assert len(rt.cfg.cameras) == 1
    # …but the URL and the frame source now carry the fresh token
    assert rt.cfg.cameras[0].frame_url.endswith("jwt=FRESH")
    assert rt.context.get_camera("cam1").frame_url.endswith("jwt=FRESH")
    assert rt.context._frame_sources["cam1"] is not src_before


def test_register_leaves_static_url_source_untouched():
    """A camera whose URL didn't change (config-file/static sources) keeps its
    existing frame source object — no needless churn."""
    rt = _runtime()
    spec = CameraSpec(camera_id="cam1", frame_url="rtsp://mtx/cam1", role="front")
    rt._register_opennvr_cameras([spec])
    src_before = rt.context._frame_sources["cam1"]
    same = CameraSpec(camera_id="cam1", frame_url="rtsp://mtx/cam1", role="front")
    assert rt._register_opennvr_cameras([same]) == []
    assert rt.context._frame_sources["cam1"] is src_before


def test_refresh_updates_boot_time_camera_specs_too():
    """Boot-time cameras enter via AppConfig, not the reconcile — their spec
    objects in the context must still pick up the fresh URL."""
    boot = CameraSpec(camera_id="cam1",
                      frame_url="http://mtx/cam1/frame.jpg?jwt=BOOT", role="front")
    rt = _runtime(cameras=[boot])
    fresh = CameraSpec(camera_id="cam1",
                       frame_url="http://mtx/cam1/frame.jpg?jwt=NEW", role="front")
    assert rt._register_opennvr_cameras([fresh]) == []
    assert rt.context.get_camera("cam1").frame_url.endswith("jwt=NEW")
    assert rt.cfg.cameras[0].frame_url.endswith("jwt=NEW")
