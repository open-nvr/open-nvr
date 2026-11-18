# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""PTZ responsiveness: what a button press costs and in what order commands
reach the camera.

Before this, every move and every stop made four requests to the camera (a
GetCapabilities lookup and the command, each sent twice for the Digest
challenge), a quick tap's stop could reach the camera before its move and
leave it turning, and a camera that stopped answering was re-scanned port
by port on every press. The Digest and lookup tests use a real local HTTP
server so httpx runs its own Digest flow.
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import HTTPException

_SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVER))

# Same catch-all logger stub the sibling suites install.
_lm = types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from services import onvif_digest_service as ods  # noqa: E402
from services import ptz_service as ps  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state():
    for d in (ods._DIGEST_AUTHS, ods._PTZ_URLS, ps._PTZ_CACHE, ps._LOCKS,
              ps._LAST_RESCAN, ps._RESOLVE_FAILED, ps._PTZ_SUPPORT):
        d.clear()
    ods._WSSE_PREFERRED_HOSTS.clear()
    yield


# ── a camera that speaks Digest ─────────────────────────────────────────


class _Camera:
    """Challenges any request without a Digest Authorization carrying its
    current nonce; ``rotate()`` retires the nonce like a camera that
    expires them. Logs (action, authorised) per request."""

    def __init__(self, has_ptz: bool = True):
        self.log: list[tuple[str, bool]] = []
        self.nonce = "n1"
        self.has_ptz = has_ptz
        cam = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                m = re.search(r"<(?:\w+:)?(GetCapabilities|ContinuousMove|Stop)\b", body)
                action = m.group(1) if m else "?"
                auth = self.headers.get("Authorization") or ""
                ok = auth.startswith("Digest") and f'nonce="{cam.nonce}"' in auth
                cam.log.append((action, ok))
                if not ok:
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        f'Digest realm="cam", nonce="{cam.nonce}", qop="auth", algorithm=MD5')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                base = f"http://127.0.0.1:{cam.port}"
                ptz = (f"<tt:PTZ><tt:XAddr>{base}/onvif/ptz_service</tt:XAddr></tt:PTZ>"
                       if cam.has_ptz else "")
                inner = (
                    "<tds:GetCapabilitiesResponse><tds:Capabilities>"
                    f"<tt:Media><tt:XAddr>{base}/onvif/media_service</tt:XAddr></tt:Media>"
                    f"{ptz}</tds:Capabilities></tds:GetCapabilitiesResponse>"
                    if action == "GetCapabilities" else f"<x:{action}Response xmlns:x='urn:x'/>")
                out = (f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                       f"<s:Body>{inner}</s:Body></s:Envelope>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def rotate(self):
        self.nonce = self.nonce + "x"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def camera():
    cam = _Camera()
    yield cam
    cam.close()


def _move(cam):
    return ods.ptz_continuous_move_digest(
        "127.0.0.1", "u", "p", "Profile_1", 0.5, 0, 0, cam.port, "http")


def test_a_press_after_the_first_is_one_request(camera):
    asyncio.run(_move(camera))
    first = len(camera.log)
    asyncio.run(_move(camera))
    asyncio.run(ods.ptz_stop_digest("127.0.0.1", "u", "p", "Profile_1", camera.port, "http"))
    # The first press learns the challenge on the PTZ-address lookup, and the
    # move already answers it; every later command is just the command.
    assert camera.log[:first] == [
        ("GetCapabilities", False), ("GetCapabilities", True), ("ContinuousMove", True)]
    assert camera.log[first:] == [("ContinuousMove", True), ("Stop", True)]


def test_a_retired_nonce_costs_one_retry_not_a_failure(camera):
    asyncio.run(_move(camera))
    n = len(camera.log)
    camera.rotate()
    asyncio.run(_move(camera))
    assert camera.log[n:] == [("ContinuousMove", False), ("ContinuousMove", True)]


def test_a_changed_password_starts_a_fresh_challenge(camera):
    asyncio.run(_move(camera))
    a = ods._digest_auth("http://127.0.0.1", "u", "p")
    b = ods._digest_auth("http://127.0.0.1", "u", "other")
    assert a is not b


def test_a_camera_without_ptz_answers_409_and_is_asked_once():
    cam = _Camera(has_ptz=False)
    try:
        for _ in range(2):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(_move(cam))
            assert exc.value.status_code == 409
            assert exc.value.detail == ods.NO_PTZ_DETAIL
        assert [a for a, ok in cam.log if ok] == ["GetCapabilities"]
    finally:
        cam.close()


# ── PTZService: ordering, rescans, what it learns ───────────────────────


KW = dict(camera_id=7, ip="10.0.0.7", username="u", password="p", camera_port=80)


def _config(monkeypatch, calls=None):
    async def find(*a, **k):
        if calls is not None:
            calls.append("scan")
        return {"scheme": "http", "port": 80, "profile_token": "P"}
    monkeypatch.setattr(ps.PTZService, "_find_working_config", staticmethod(find))


def test_a_quick_taps_stop_always_reaches_the_camera_last(monkeypatch):
    """Commands to one camera run in the order they arrived, however long
    the camera takes over each. Without the lock a jittery camera finished
    the stop first on a share of quick taps."""
    _config(monkeypatch)
    seen: list[str] = []
    rng = random.Random(1)

    async def move(*a, **k):
        await asyncio.sleep(rng.uniform(0.0, 0.03))
        seen.append("move")

    async def stop(*a, **k):
        await asyncio.sleep(rng.uniform(0.0, 0.03))
        seen.append("stop")

    monkeypatch.setattr(ps, "ptz_continuous_move_digest", move)
    monkeypatch.setattr(ps, "ptz_stop_digest", stop)

    async def taps():
        for _ in range(30):
            mv = asyncio.create_task(ps.PTZService.move(**KW, x=0.5, y=0, z=0))
            await asyncio.sleep(0)
            st = asyncio.create_task(ps.PTZService.stop(**KW))
            await asyncio.gather(mv, st)
    asyncio.run(taps())
    assert seen == ["move", "stop"] * 30


def test_no_ptz_is_remembered_and_never_rescanned(monkeypatch):
    calls: list[str] = []
    _config(monkeypatch, calls)

    async def move(*a, **k):
        raise HTTPException(status_code=409, detail=ods.NO_PTZ_DETAIL)

    monkeypatch.setattr(ps, "ptz_continuous_move_digest", move)
    for _ in range(3):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ps.PTZService.move(**KW, x=0.5, y=0, z=0))
        assert exc.value.status_code == 409
    assert calls == ["scan"]
    assert ps.ptz_support(7) is False


def test_a_working_command_marks_the_camera_ptz_capable(monkeypatch):
    _config(monkeypatch)

    async def ok(*a, **k):
        return None

    monkeypatch.setattr(ps, "ptz_stop_digest", ok)
    assert ps.ptz_support(7) is None
    asyncio.run(ps.PTZService.stop(**KW))
    assert ps.ptz_support(7) is True


def test_a_failing_camera_is_rescanned_once_a_minute_not_per_press(monkeypatch):
    calls: list[str] = []
    _config(monkeypatch, calls)

    async def refused(*a, **k):
        raise HTTPException(status_code=503, detail="Cannot connect to ONVIF device")

    monkeypatch.setattr(ps, "ptz_continuous_move_digest", refused)
    for _ in range(4):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ps.PTZService.move(**KW, x=0.5, y=0, z=0))
        assert exc.value.status_code == 503
    # One scan to start from; the failures right after it do not scan again.
    assert calls == ["scan"]

    ps._LAST_RESCAN[7] -= ps.RESCAN_COOLDOWN_S + 1
    with pytest.raises(HTTPException):
        asyncio.run(ps.PTZService.move(**KW, x=0.5, y=0, z=0))
    assert calls == ["scan", "scan"]


def test_a_camera_that_cannot_be_found_is_not_scanned_on_every_press(monkeypatch):
    calls: list[str] = []

    async def find(*a, **k):
        calls.append("scan")
        raise HTTPException(status_code=500, detail="Failed to find ONVIF profile")

    monkeypatch.setattr(ps.PTZService, "_find_working_config", staticmethod(find))
    for _ in range(3):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ps.PTZService.stop(**KW))
        assert exc.value.status_code == 500
    assert calls == ["scan"]


def test_a_timeout_does_not_trigger_a_port_scan(monkeypatch):
    calls: list[str] = []
    _config(monkeypatch, calls)
    ps._LAST_RESCAN.clear()

    async def slow(*a, **k):
        raise HTTPException(status_code=504, detail="ONVIF request timeout")

    monkeypatch.setattr(ps, "ptz_stop_digest", slow)
    ps._PTZ_CACHE[7] = {"scheme": "http", "port": 80, "profile_token": "P", "ip": "10.0.0.7"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ps.PTZService.stop(**KW))
    assert exc.value.status_code == 504
    assert calls == []
