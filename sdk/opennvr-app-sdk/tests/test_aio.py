# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``AsyncOpenNVR`` — the async twin of the platform client: the same
routes with the app key, the same degrade/raise rules, and a parity
check so the two surfaces cannot drift apart."""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from opennvr_app_sdk import OpenNVR, PlatformError, Recording
from opennvr_app_sdk.aio import (
    AsyncAIAPI, AsyncAlertsAPI, AsyncOpenNVR, AsyncRecordingsAPI,
    AsyncStateAPI, AsyncTimelineAPI,
)
from opennvr_app_sdk.client import (
    AIAPI, AlertsAPI, RecordingsAPI, StateAPI, TimelineAPI,
)
from opennvr_app_sdk.frame_app import KaiCError
from test_client import APP_KEY, _FakeCore


class _FakeKaiC(BaseHTTPRequestHandler):
    log: list[dict] = []

    def _reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self.log.append({"path": self.path, "key": self.headers.get("X-Internal-Api-Key")})
        if self.path == "/api/v1/ai/capabilities":
            return self._reply(200, {"adapters": {"yolov8": {"tasks_advertised": ["object_detection"]}}})
        return self._reply(404, {})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self.log.append({"path": self.path, "body": body,
                         "corr": self.headers.get("X-Correlation-Id")})
        if self.path.endswith("/broken"):
            return self._reply(500, {"detail": "boom"})
        return self._reply(200, {"result": {"detections": [{"label": "person"}]},
                                 "inference_ms": 3})

    def log_message(self, *a):
        pass


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("OPENNVR_APP_KEY", APP_KEY)
    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    _FakeCore.log, _FakeCore.state, _FakeKaiC.log = [], {}, []
    core = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCore)
    kaic = ThreadingHTTPServer(("127.0.0.1", 0), _FakeKaiC)
    for s in (core, kaic):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        yield (f"http://127.0.0.1:{core.server_address[1]}",
               f"http://127.0.0.1:{kaic.server_address[1]}")
    finally:
        core.shutdown()
        kaic.shutdown()


def test_cameras_snapshot_recordings(stack):
    core, _ = stack

    async def go():
        async with AsyncOpenNVR(core) as nvr:
            cams = await nvr.cameras()
            assert [c.id for c in cams] == [1, 2] and cams[0].has_skill("loitering")
            assert (await nvr.camera("cam2")).name == "Yard"
            assert await nvr.camera(99) is None
            assert await nvr.snapshot(cams[0]) == b"\xff\xd8jpeg"
            assert await nvr.snapshot("cam2") is None
            recs = await nvr.recordings(1).list(start="2026-09-05T00:00:00Z")
            assert recs == [Recording(start="2026-09-05T10:00:00Z", duration=60.0)]
            url = await nvr.recordings("cam-1").url("2026-09-05T10:00:00Z", 60)
            assert url.startswith("http://mediamtx")

    asyncio.run(go())
    assert all(r["key"] == APP_KEY for r in _FakeCore.log)


def test_timeline_alerts_state(stack):
    core, _ = stack

    async def go():
        nvr = AsyncOpenNVR(core)
        assert await nvr.timeline.search(camera="cam1", label="car", limit=5) == \
            [{"id": 9, "camera_id": 1, "label": "car"}]
        assert _FakeCore.log[-1]["query"] == {"camera_id": "1", "label": "car", "limit": "5"}
        assert await nvr.timeline.evidence(9) == b"\xff\xd8ev"
        assert await nvr.timeline.plate_stats(days=3) == {"total_reads": 3}
        assert await nvr.alerts.inbox(unacked=True) == [{"id": 1, "acknowledged_at": None}]
        assert await nvr.state.get("missing", default="d") == "d"
        await nvr.state.set("cooldown", {"cam1": 12.5})
        assert await nvr.state.get("cooldown") == {"cam1": 12.5}
        assert await nvr.state.items() == {"cooldown": {"cam1": 12.5}}
        assert await nvr.state.delete("cooldown") is True
        assert await nvr.state.delete("cooldown") is False
        with pytest.raises(PlatformError):
            await nvr.state.set("toolarge", "x")
        await nvr.aclose()

    asyncio.run(go())


def test_ai_capabilities_and_infer(stack):
    core, kaic = stack

    async def go():
        async with AsyncOpenNVR(core, kaic_url=kaic, kaic_api_key="k") as nvr:
            caps = await nvr.ai.capabilities()
            assert "yolov8" in caps["adapters"]
            assert _FakeKaiC.log[-1]["key"] == "k"
            out = await nvr.ai.infer("yolov8", b"\xff\xd8", task="object_detection",
                                     camera_id="cam1", params={"threshold": 0.4},
                                     correlation_id="corr-1")
            assert out["result"]["detections"][0]["label"] == "person"
            sent = _FakeKaiC.log[-1]
            assert sent["path"] == "/api/v1/infer/yolov8" and sent["corr"] == "corr-1"
            assert sent["body"]["task"] == "object_detection"
            assert sent["body"]["camera_id"] == "cam1" and sent["body"]["threshold"] == 0.4
            assert sent["body"]["frame_b64"] == "/9g="
            with pytest.raises(KaiCError):
                await nvr.ai.infer("broken", b"\xff\xd8", task="object_detection")

    asyncio.run(go())


def test_unreachable_reads_degrade_writes_raise(monkeypatch):
    monkeypatch.setenv("OPENNVR_APP_KEY", APP_KEY)

    async def go():
        nvr = AsyncOpenNVR("http://127.0.0.1:9", timeout=0.2)
        assert await nvr.cameras() == [] and await nvr.snapshot(1) is None
        assert await nvr.timeline.search() is None
        assert await nvr.ai.capabilities() is None          # no KAIC_URL
        with pytest.raises(PlatformError):
            await nvr.ai.infer("x", b"", task="t")
        with pytest.raises(PlatformError):
            await nvr.state.set("k", 1)
        await nvr.aclose()

    asyncio.run(go())


def test_shared_http_client_is_not_closed(monkeypatch):
    import httpx

    monkeypatch.setenv("OPENNVR_APP_KEY", APP_KEY)

    async def go():
        shared = httpx.AsyncClient()
        async with AsyncOpenNVR("http://127.0.0.1:9", http_client=shared):
            pass
        assert not shared.is_closed
        await shared.aclose()

    asyncio.run(go())


def test_requires_a_url(monkeypatch):
    monkeypatch.delenv("OPENNVR_URL", raising=False)
    with pytest.raises(ValueError):
        AsyncOpenNVR()


# ── parity: every public sync method has an async twin with the same
#    signature (so a feature added to one cannot be forgotten on the other)


@pytest.mark.parametrize("sync_cls,async_cls", [
    (OpenNVR, AsyncOpenNVR), (RecordingsAPI, AsyncRecordingsAPI),
    (TimelineAPI, AsyncTimelineAPI), (AlertsAPI, AsyncAlertsAPI),
    (StateAPI, AsyncStateAPI), (AIAPI, AsyncAIAPI),
])
def test_async_surface_mirrors_sync(sync_cls, async_cls):
    renamed = {"close": "aclose", "__enter__": "__aenter__", "__exit__": "__aexit__"}
    not_ported = {"stream"}       # blocking WebSocket session — documented
    for name, member in inspect.getmembers(sync_cls, inspect.isfunction):
        if name.startswith("_") and name not in renamed or name in not_ported:
            continue
        twin = renamed.get(name, name)
        assert hasattr(async_cls, twin), f"{async_cls.__name__} lacks {twin}"
        if name in renamed or name == "__init__":
            continue
        sp = list(inspect.signature(member).parameters)
        ap = list(inspect.signature(getattr(async_cls, twin)).parameters)
        assert sp == ap, f"{sync_cls.__name__}.{name}{sp} != {async_cls.__name__}.{twin}{ap}"
