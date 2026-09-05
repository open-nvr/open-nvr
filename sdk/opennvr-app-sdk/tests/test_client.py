# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``OpenNVR`` — the platform client — against a fake core: every
method hits the route it claims with the app's credential, and reads
degrade to None/[] while writes raise."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from opennvr_app_sdk import Camera, OpenNVR, PlatformError, Recording

APP_KEY = "oak_my-app_" + "1" * 32


class _FakeCore(BaseHTTPRequestHandler):
    """Records every request; answers a canned body per path."""

    log: list[dict] = []
    state: dict[str, object] = {}

    def _reply(self, status, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _note(self, body=None):
        u = urlparse(self.path)
        self.log.append({"method": self.command, "path": u.path,
                         "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                         "key": self.headers.get("X-Internal-Api-Key"), "body": body})
        return u

    def do_GET(self):  # noqa: N802
        u = self._note()
        p = u.path
        if p == "/api/v1/internal/camera-agent/cameras":
            return self._reply(200, {"cameras": [
                {"camera_id": "cam1", "open_nvr_camera_id": "1", "name": "Gate",
                 "role": "Gate; location: front", "frame_url": "rtsp://tap/cam-1",
                 "assignments": [{"skill": "loitering"}]},
                {"camera_id": "cam2", "open_nvr_camera_id": "2", "name": "Yard",
                 "role": "Yard", "frame_url": "rtsp://tap/cam-2"}]})
        if p == "/api/v1/internal/app/cameras/1/snapshot":
            return self._reply(200, b"\xff\xd8jpeg", "image/jpeg")
        if p == "/api/v1/internal/app/cameras/2/snapshot":
            return self._reply(503, {"detail": "offline"})
        if p == "/api/v1/internal/app/recordings/1":
            return self._reply(200, {"recordings": [{"start": "2026-09-05T10:00:00Z",
                                                     "duration": 60.0}]})
        if p == "/api/v1/internal/app/recordings/1/url":
            return self._reply(200, {"url": "http://mediamtx:9996/get?path=cam-1"})
        if p == "/api/v1/internal/camera-agent/events":
            return self._reply(200, {"events": [{"id": 9, "camera_id": 1, "label": "car"}]})
        if p == "/api/v1/internal/camera-agent/events/9/evidence":
            return self._reply(200, b"\xff\xd8ev", "image/jpeg")
        if p == "/api/v1/internal/app/plates/stats":
            return self._reply(200, {"total_reads": 3})
        if p == "/api/v1/internal/app/alerts":
            return self._reply(200, {"alerts": [{"id": 1, "acknowledged_at": None}]})
        if p == "/api/v1/internal/app/state":
            return self._reply(200, {"items": [{"key": k, "value": v}
                                               for k, v in self.state.items()]})
        if p.startswith("/api/v1/internal/app/state/"):
            key = p.rsplit("/", 1)[1]
            if key in self.state:
                return self._reply(200, {"key": key, "value": self.state[key]})
            return self._reply(404, {"detail": "No such key"})
        return self._reply(404, {"detail": "nope"})

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"null")
        u = self._note(body)
        key = u.path.rsplit("/", 1)[1]
        if key == "toolarge":
            return self._reply(413, {"detail": "value over cap"})
        self.state[key] = body
        return self._reply(200, {"key": key, "value": body})

    def do_DELETE(self):  # noqa: N802
        u = self._note()
        key = u.path.rsplit("/", 1)[1]
        return self._reply(200, {"deleted": self.state.pop(key, None) is not None})

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def core(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENNVR_APP_KEY", APP_KEY)
    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    _FakeCore.log = []
    _FakeCore.state = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCore)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def test_cameras_snapshot_recordings(core):
    with OpenNVR(core) as nvr:
        cams = nvr.cameras()
        assert [c.id for c in cams] == [1, 2] and cams[0].handle == "cam1"
        assert cams[0].has_skill("loitering") and not cams[1].has_skill("loitering")
        assert nvr.camera("cam2").name == "Yard" and nvr.camera(99) is None
        assert nvr.snapshot(cams[0]) == b"\xff\xd8jpeg"
        assert nvr.snapshot("cam2") is None          # 503 → None, logged
        recs = nvr.recordings(1).list(start="2026-09-05T00:00:00Z")
        assert recs == [Recording(start="2026-09-05T10:00:00Z", duration=60.0)]
        assert nvr.recordings("cam-1").url("2026-09-05T10:00:00Z", 60).startswith("http://mediamtx")
    # Every call carried the APP key, never the site key.
    assert all(r["key"] == APP_KEY for r in _FakeCore.log)
    listing = next(r for r in _FakeCore.log if r["path"].endswith("/recordings/1"))
    assert listing["query"] == {"start": "2026-09-05T00:00:00Z"}


def test_timeline_and_alerts(core):
    nvr = OpenNVR(core)
    ev = nvr.timeline.search(camera="cam1", label="car", limit=5)
    assert ev == [{"id": 9, "camera_id": 1, "label": "car"}]
    assert _FakeCore.log[-1]["query"] == {"camera_id": "1", "label": "car", "limit": "5"}
    assert nvr.timeline.evidence(9) == b"\xff\xd8ev"
    assert nvr.timeline.plate_stats(days=3) == {"total_reads": 3}
    assert nvr.alerts.inbox(unacked=True) == [{"id": 1, "acknowledged_at": None}]
    assert _FakeCore.log[-1]["query"]["unacked"] == "true"


def test_state_roundtrip_and_write_errors(core):
    nvr = OpenNVR(core)
    assert nvr.state.get("missing", default="dflt") == "dflt"
    nvr.state.set("cooldown", {"cam1": 12.5})
    assert nvr.state.get("cooldown") == {"cam1": 12.5}
    assert nvr.state.items() == {"cooldown": {"cam1": 12.5}}
    assert nvr.state.delete("cooldown") is True and nvr.state.delete("cooldown") is False
    with pytest.raises(PlatformError):
        nvr.state.set("toolarge", "x")


def test_unreachable_core_reads_degrade_writes_raise(monkeypatch):
    monkeypatch.setenv("OPENNVR_APP_KEY", APP_KEY)
    nvr = OpenNVR("http://127.0.0.1:9", timeout=0.2)     # nothing listens
    assert nvr.cameras() == [] and nvr.snapshot(1) is None
    assert nvr.timeline.search() is None                 # "couldn't check", not "empty"
    with pytest.raises(PlatformError):
        nvr.state.set("k", 1)


def test_requires_a_url(monkeypatch):
    monkeypatch.delenv("OPENNVR_URL", raising=False)
    with pytest.raises(ValueError):
        OpenNVR()


def test_camera_id_parsing():
    from opennvr_app_sdk.client import _camera_id
    assert _camera_id("cam7") == _camera_id("CAM-7") == _camera_id(7) == 7
    assert _camera_id(Camera(id=3, handle="cam3", name="", role="", frame_url="")) == 3
    with pytest.raises(ValueError):
        _camera_id("gate")
