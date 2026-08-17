# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""describe_camera must look at a FRESH LIVE frame for a static/quiet scene.

Regression: describe_camera preferred Tier-0's best frame unconditionally. The
best frame is a curated crop of a RECENT detection, so for a person sitting
STILL (no fresh track) it is a PAST frame — the agent described an earlier
moment and missed the person actually in view. It must use the best frame only
while a track is currently active, and take a live grab otherwise.
"""
from __future__ import annotations

import asyncio
import time
import types

from context import CameraContext, CameraSpec
from tools import CameraTools


class _LiveSource:
    camera_id = "cam1"
    def fetch(self):
        return b"LIVE-FRAME"


class _EchoCaptioner:
    """Records which frame bytes reached the VLM."""
    def __init__(self):
        self.frame = None
    async def infer(self, *, frame_jpeg, extra=None, correlation_id=None):
        self.frame = frame_jpeg
        return {"result": {"caption": "a scene"}}


def _tools(caption, *, latest_event):
    ctx = CameraContext(cameras=[CameraSpec(camera_id="cam1", frame_url="x", role="r")])
    ctx.register_frame_source("cam1", _LiveSource())
    # Control the Tier-0 freshness signal.
    ctx.latest_inference = lambda cam, adapter=None: latest_event
    async def best_frame_fetch(cam):
        return b"BEST-FRAME-STALE"
    return CameraTools(
        context=ctx, detection_client=None, caption_client=caption,
        recognition_client=None, footage_index=None,
        best_frame_fetch=best_frame_fetch,
    )


def _event(age_seconds):
    return types.SimpleNamespace(received_at=time.time() - age_seconds, raw={})


def test_no_active_track_uses_live_frame():
    # Person sitting still -> no Tier-0 event -> must take a LIVE look.
    cap = _EchoCaptioner()
    asyncio.run(_tools(cap, latest_event=None).describe_camera({"camera_id": "cam1"}))
    assert cap.frame == b"LIVE-FRAME"


def test_stale_track_uses_live_frame():
    # Newest track is 30s old (> 10s window) -> stale -> live look, not best frame.
    cap = _EchoCaptioner()
    asyncio.run(_tools(cap, latest_event=_event(30)).describe_camera({"camera_id": "cam1"}))
    assert cap.frame == b"LIVE-FRAME"


def test_active_track_uses_best_frame():
    # Something actively happening (2s old) -> best frame preserved (efficiency win).
    cap = _EchoCaptioner()
    asyncio.run(_tools(cap, latest_event=_event(2)).describe_camera({"camera_id": "cam1"}))
    assert cap.frame == b"BEST-FRAME-STALE"


def test_vqa_question_on_static_scene_uses_live_frame():
    # "what is he wearing?" on a static scene must still look live, not at a
    # stale best frame of an earlier object.
    cap = _EchoCaptioner()
    asyncio.run(_tools(cap, latest_event=None).describe_camera(
        {"camera_id": "cam1", "question": "what is he wearing?"}))
    assert cap.frame == b"LIVE-FRAME"
