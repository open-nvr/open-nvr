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


def test_vision_error_reason_separates_warming_up_from_unreachable():
    """A 503 is the adapter ANSWERING that it isn't ready — the opposite of
    unreachable. Calling the auto-pull window "unreachable" sent an operator
    hunting Docker networking while a multi-GB model was simply downloading."""
    from tools import CameraTools as _T

    r = _T._vision_error_reason
    pulling = RuntimeError(
        "Server error '503 Service Unavailable' for url "
        "'http://opennvr-core:8100/api/v1/infer/ollamavlm' — KAI-C detail: "
        "{'status': 'error', 'error': {'category': 'model_error', 'code': "
        "'model_not_pulled', 'message': \"Ollama has no model 'gemma3:4b' "
        "(auto-pull is running — retry shortly)\", 'transient': True, "
        "'retry_after_ms': 5000}}")
    reason = r(pulling)
    assert "still downloading" in reason
    assert "unreachable" not in reason

    warming = r(RuntimeError("Server error '503 Service Unavailable'"))
    assert "warming up" in warming and "unreachable" not in warming

    # A 502 means KAI-C's own 30s proxy budget expired: adapter down OR a
    # VLM too slow for this host. Both, because the operator can't tell.
    slow = r(RuntimeError(
        "Server error '502 Bad Gateway' — KAI-C detail: adapter unreachable: "))
    assert "no answer in time" in slow and "too slow" in slow

    # An unclassified failure still reads as unreachable.
    assert r(RuntimeError("connection refused")) == "caption adapter unreachable"


def test_infer_backoff_honours_server_retry_after():
    """KAI-C's transient errors carry retry_after_ms. Ignoring it meant three
    attempts 1.5s apart — ~3s against a multi-GB pull, so the cold-start
    window the retry loop exists for was never actually bridged."""
    import httpx
    from adapter_clients import KaicAdapterClient

    c = KaicAdapterClient(kaic_url="http://kaic", api_key="k",
                          adapter_name="ollamavlm", retry_backoff_s=1.5)

    def _resp(payload):
        return httpx.Response(503, json=payload,
                              request=httpx.Request("POST", "http://kaic"))

    assert c._backoff_for(None) == 1.5                      # no response
    assert c._backoff_for(_resp({"detail": "plain string"})) == 1.5
    assert c._backoff_for(_resp({"detail": {"error": {}}})) == 1.5
    assert c._backoff_for(
        _resp({"detail": {"error": {"retry_after_ms": 5000}}})) == 5.0
    # Never shorter than the local floor, never long enough to stall a turn.
    assert c._backoff_for(
        _resp({"detail": {"error": {"retry_after_ms": 200}}})) == 1.5
    assert c._backoff_for(
        _resp({"detail": {"error": {"retry_after_ms": 9_000_000}}})) == 10.0
