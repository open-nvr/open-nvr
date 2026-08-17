# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""describe_window: narrate WHAT HAPPENED over a past window from the recorded
footage — sample a few frames across [start,end] and caption each. Tier 3 of
the historical-understanding ladder (index -> still -> footage)."""
from __future__ import annotations

import asyncio

from context import CameraContext, CameraSpec
from tools import CameraTools, build_tool_definitions


class _FakeEvents:
    """Serves recording frames per instant; records the instants requested."""
    def __init__(self, frame=b"FRAME", frames_by_call=None):
        self._frame = frame
        self._by_call = frames_by_call  # list[bytes|None] to vary per call
        self.calls = []
    async def recording_frame(self, camera_id, at):
        self.calls.append((camera_id, at))
        if self._by_call is not None:
            i = len(self.calls) - 1
            return self._by_call[i] if i < len(self._by_call) else None
        return self._frame
    async def evidence(self, event_id):
        return None


class _Captioner:
    """Returns scripted captions in order (last repeats)."""
    def __init__(self, captions):
        self._c = captions
        self.i = 0
        self.frames = []
    async def infer(self, *, frame_jpeg, extra=None, correlation_id=None):
        self.frames.append(frame_jpeg)
        cap = self._c[min(self.i, len(self._c) - 1)]
        self.i += 1
        return {"result": {"caption": cap}}


def _tools(events, caption):
    ctx = CameraContext(cameras=[CameraSpec(camera_id="cam1", frame_url="x", role="r")])
    return CameraTools(
        context=ctx, detection_client=None, caption_client=caption,
        recognition_client=None, footage_index=None, events_client=events,
        resolve_camera=lambda c: "3",   # agent cam -> server-side id
    )


WIN = {"camera_id": "cam1",
       "start_time": "2026-08-14T15:12:00+00:00",
       "end_time": "2026-08-14T15:16:00+00:00"}


def test_narrates_behavior_across_the_window():
    ev = _FakeEvents(frame=b"F")
    cap = _Captioner(["a person enters", "a person enters",
                      "person at the desk", "person at the desk", "empty room"])
    out = asyncio.run(_tools(ev, cap).describe_window(dict(WIN)))
    # Samples the window on the server-side camera id, and collapses repeats.
    assert all(c[0] == 3 for c in ev.calls)
    assert len(ev.calls) == CameraTools._WINDOW_SAMPLES
    assert "a person enters" in out and "person at the desk" in out and "empty room" in out
    # consecutive duplicates collapsed -> three distinct beats, not five
    assert out.count(";") == 2


def test_no_footage_reports_cleanly():
    ev = _FakeEvents(frames_by_call=[None, None, None, None, None])
    out = asyncio.run(_tools(ev, _Captioner(["x"])).describe_window(dict(WIN)))
    assert "couldn't pull footage" in out


def test_bad_times_rejected():
    ev = _FakeEvents()
    bad = {"camera_id": "cam1", "start_time": "nope", "end_time": "also-nope"}
    out = asyncio.run(_tools(ev, _Captioner(["x"])).describe_window(bad))
    assert "valid start_time and end_time" in out


def test_end_before_start_rejected():
    ev = _FakeEvents()
    rev = {"camera_id": "cam1",
           "start_time": "2026-08-14T15:16:00+00:00",
           "end_time": "2026-08-14T15:12:00+00:00"}
    out = asyncio.run(_tools(ev, _Captioner(["x"])).describe_window(rev))
    assert "end time must be after" in out


def test_unknown_camera_rejected():
    ev = _FakeEvents()
    out = asyncio.run(_tools(ev, _Captioner(["x"])).describe_window(
        {**WIN, "camera_id": "nope"}))
    assert "specific camera" in out


def test_describe_window_advertised():
    names = [d["function"]["name"] for d in build_tool_definitions(["cam1"])]
    assert "describe_window" in names
