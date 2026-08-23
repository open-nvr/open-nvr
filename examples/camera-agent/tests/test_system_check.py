# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""System self-check: the runner's guarantees, and honest degradation in
describe answers. The disease both treat is silent degradation — a broken
capability path hiding behind healthy-looking processes and confident text."""
from __future__ import annotations

import asyncio

import pytest

from system_check import DEGRADED, DOWN, OK, run_checks


def test_runner_statuses_latency_and_summary():
    async def ok():
        return OK, "fine"

    async def degraded():
        return DEGRADED, "meh"

    async def boom():
        raise RuntimeError("403 Forbidden from KAI-C")

    report = asyncio.run(run_checks(
        {"a": ok, "b": degraded, "c": boom}, timeout_s=2.0))
    by = {r.name: r for r in report.results}
    assert by["a"].status == OK and by["b"].status == DEGRADED
    assert by["c"].status == DOWN and "403 Forbidden" in by["c"].detail
    assert not report.healthy
    assert report.summary == "1 down, 1 degraded"
    assert all(r.latency_ms >= 0 for r in report.results)
    # board lines carry the icon + detail
    assert any(line.startswith("❌ c:") for line in report.lines())


def test_runner_times_out_hung_check_without_wedging():
    async def hang():
        await asyncio.sleep(30)
        return OK, "never"

    report = asyncio.run(run_checks({"hung": hang}, timeout_s=0.05))
    (r,) = report.results
    assert r.status == DOWN and "no answer" in r.detail


def test_runner_normalizes_bogus_status():
    async def weird():
        return "amazing", "detail"

    report = asyncio.run(run_checks({"w": weird}))
    assert report.results[0].status == DOWN


# ── honest degradation in describe ─────────────────────────────────


class _RaisingCaption:
    async def infer(self, **kw):
        raise RuntimeError("Client error '403 Forbidden' for url 'http://kaic/infer/ollamavlm'")


class _Detect:
    def __init__(self, detections):
        self._d = detections
    async def infer(self, **kw):
        return {"result": {"detections": self._d}}


def _executor(caption, detect):
    from context import CameraContext, CameraSpec
    from tools import CameraTools as ToolExecutor

    ctx = CameraContext(cameras=[CameraSpec("cam1", "http://x/f.jpg", "front")])

    class _Src:
        def fetch(self):
            return b"\xff\xd8jpeg"
    ctx.register_frame_source("cam1", _Src())
    return ToolExecutor(context=ctx, caption_client=caption,
                        detection_client=detect, recognition_client=detect)


def test_describe_fallback_admits_vision_is_unavailable():
    ex = _executor(_RaisingCaption(),
                   _Detect([{"label": "person", "score": 0.9}]))
    out = asyncio.run(ex.describe_camera({"camera_id": "cam1"}))
    assert "person" in out
    assert "vision model is unavailable" in out
    assert "refused by KAI-C (403)" in out        # the 403 is named, not hidden
    assert ex.last_vision_error and "403" in ex.last_vision_error


def test_describe_fallback_empty_scene_still_admits_degradation():
    ex = _executor(_RaisingCaption(), _Detect([]))
    out = asyncio.run(ex.describe_camera({"camera_id": "cam1"}))
    assert "no objects detected" in out
    assert "vision model is unavailable" in out


def test_describe_healthy_path_unchanged():
    class _Caption:
        async def infer(self, **kw):
            return {"result": {"caption": "a person at the door"}}
    ex = _executor(_Caption(), _Detect([]))
    out = asyncio.run(ex.describe_camera({"camera_id": "cam1"}))
    assert out == "On cam1: a person at the door."   # _join_clauses phrasing
    assert "unavailable" not in out


def test_vision_error_reason_names_the_actual_gate():
    """A 403 is not one thing: the reason names WHICH gate refused. The
    field cost of a bare 403 guess ('sovereignty?') was a multi-hour hunt
    that ended at the permission-approval gate."""
    from tools import CameraTools as _T

    r = _T._vision_error_reason
    approval = RuntimeError(
        "Client error '403 Forbidden' — KAI-C detail: adapter 'ollamavlm' is "
        "pending_approval: 1 declared permission(s) await operator approval "
        "before it may serve inference")
    assert "operator approval" in r(approval)
    sovereignty = RuntimeError(
        "403 — KAI-C detail: AI_SOVEREIGNTY=local_only refuses adapter: "
        "declared network_egress entry is not on this machine")
    assert "sovereignty" in r(sovereignty)
    assert r(RuntimeError("Client error '403 Forbidden'")) == "refused by KAI-C (403)"
    assert "timed out" in r(RuntimeError("request timed out"))
