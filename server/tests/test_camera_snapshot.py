# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""GET /cameras/{id}/snapshot — the geometry editors' backdrop.

Calls the endpoint COROUTINE directly with mocked collaborators
(CameraService + the KAI-C capture service) rather than mounting the
FastAPI app through TestClient. That keeps the test independent of the
suite-wide ``core.logging_config`` / ``core.auth`` stubbing games some
sibling modules play (which otherwise give the route a different
dependency-function identity and defeat dependency_overrides). No
OpenCV, no RTSP, no DB — just the handler's own logic.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
try:
    from cryptography.fernet import Fernet

    os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")

# routers.cameras imports domain loggers from core.logging_config. A
# sibling test module may have installed a minimal STUB of that module
# (via setdefault) lacking some loggers/methods our import path needs.
# Backfill defensively so `from routers import cameras` works whichever
# module's stub won — without displacing it (setdefault-friendly).
import core.logging_config as _lc  # noqa: E402


class _L:  # permissive no-op logger
    def __getattr__(self, _n):
        return lambda *a, **kw: None


_LOG_METHODS = ("info", "warning", "error", "debug", "exception",
                "critical", "log", "log_action")
for _name in (
    "main_logger", "auth_logger", "camera_logger", "recording_logger",
    "rtsp_logger", "api_logger", "mediamtx_logger", "config_logger",
    "storage_logger", "stream_logger", "ai_logger", "system_logger",
    "security_logger",
):
    _obj = getattr(_lc, _name, None)
    if _obj is None:
        setattr(_lc, _name, _L())
        continue
    for _m in _LOG_METHODS:
        if not hasattr(_obj, _m):
            try:
                setattr(_obj, _m, lambda *a, **kw: None)
            except (AttributeError, TypeError):
                pass
if not hasattr(_lc, "setup_logging"):
    _lc.setup_logging = lambda *a, **kw: None

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import Response  # noqa: E402

from routers import cameras as cameras_router  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"fakejpegbytes" * 4


def _camera(cid=7, rtsp_url="rtsp://gate/stream"):
    return types.SimpleNamespace(id=cid, rtsp_url=rtsp_url)


def _user():
    return types.SimpleNamespace(id=1, is_superuser=True)


def _call(monkeypatch, *, camera=_camera(), jpeg=JPEG, get_by_id_raises=None):
    """Invoke the endpoint coroutine with mocked collaborators. Returns
    the Response, or raises whatever HTTPException the handler raises."""
    from services.camera_service import CameraService

    calls: list = []

    class _Session:
        closed = False

        def close(self):
            self.closed = True

    db = _Session()

    def _get_by_id(db, camera_id, user_id):
        if get_by_id_raises is not None:
            raise get_by_id_raises
        return camera

    class _StubKaiC:
        async def capture_frame_bytes(self, rtsp_url, camera_id):
            # The capture can wait out a 30 s stream timeout; the request's
            # connection must already be back in the pool by then.
            assert db.closed, "the DB session was held through the capture"
            calls.append((rtsp_url, camera_id))
            return jpeg

    monkeypatch.setattr(CameraService, "get_camera_by_id", staticmethod(_get_by_id))
    # The handler resolves get_kai_c_service via a late import from
    # services.kai_c_service — patch it there.
    import services.kai_c_service as kmod

    monkeypatch.setattr(kmod, "get_kai_c_service", lambda: _StubKaiC())

    resp = asyncio.run(
        cameras_router.get_camera_snapshot(
            camera_id=getattr(camera, "id", 7), db=db, current_user=_user()
        )
    )
    return resp, calls


def test_snapshot_returns_fresh_jpeg(monkeypatch):
    resp, calls = _call(monkeypatch)
    assert isinstance(resp, Response)
    assert resp.media_type == "image/jpeg"
    assert resp.headers["cache-control"] == "no-store"  # editors want NOW
    assert resp.body == JPEG
    assert calls == [("rtsp://gate/stream", 7)]


def test_snapshot_unknown_camera_404(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, camera=None)
    assert ei.value.status_code == 404


def test_snapshot_no_stream_url_503(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, camera=_camera(rtsp_url=None))
    assert ei.value.status_code == 503


def test_snapshot_capture_failure_503(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, jpeg=None)
    assert ei.value.status_code == 503


def test_snapshot_ownership_denial_propagates(monkeypatch):
    """A non-owner's access denial from CameraService surfaces as the
    same HTTPException the service raises (403), not a swallowed 200."""
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, get_by_id_raises=HTTPException(status_code=403, detail="no"))
    assert ei.value.status_code == 403
