# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Clip export probes the upstream status BEFORE committing a response.

Regression cover: export_clip used to check the MediaMTX status inside the
StreamingResponse generator, after a 200 + Content-Disposition had already
been sent — so an upstream failure produced a silent, zero-byte 'clip.mp4'
download. It must instead surface 404 (no clip / bad range) or 502 (upstream
error) and only stream on a real 200.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

import routers.recordings as rr  # noqa: E402


class FakeResp:
    def __init__(self, status):
        self.status_code = status
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def aiter_bytes(self, chunk_size=0):
        yield b"video-bytes"


class FakeClient:
    def __init__(self, status):
        self._status = status
        self.last_resp = None

    def build_request(self, *a, **k):
        return object()

    async def send(self, req, stream=False):
        self.last_resp = FakeResp(self._status)
        return self.last_resp


def _mint_ticket() -> str:
    t = secrets.token_hex(8)
    rr._export_tickets[t] = {
        "expires": time.time() + 300,
        "path": "cam-1",
        "start": "2026-08-14T13:00:00Z",
        "duration": 60.0,
        "filename": "clip.mp4",
    }
    return t


def _run(status):
    fake = FakeClient(status)
    rr.mediamtx_client.get_client = lambda: fake
    ticket = _mint_ticket()
    return fake, asyncio.get_event_loop().run_until_complete, ticket


def test_upstream_404_raises_404_not_silent_download():
    fake = FakeClient(404)
    rr.mediamtx_client.get_client = lambda: fake
    ticket = _mint_ticket()
    with pytest.raises(HTTPException) as ei:
        asyncio.new_event_loop().run_until_complete(rr.export_clip(ticket))
    assert ei.value.status_code == 404
    assert fake.last_resp.closed is True  # stream closed on the failure path


def test_upstream_500_raises_502():
    fake = FakeClient(500)
    rr.mediamtx_client.get_client = lambda: fake
    ticket = _mint_ticket()
    with pytest.raises(HTTPException) as ei:
        asyncio.new_event_loop().run_until_complete(rr.export_clip(ticket))
    assert ei.value.status_code == 502


def test_upstream_200_returns_streaming_response():
    fake = FakeClient(200)
    rr.mediamtx_client.get_client = lambda: fake
    ticket = _mint_ticket()
    result = asyncio.new_event_loop().run_until_complete(rr.export_clip(ticket))
    assert isinstance(result, StreamingResponse)
    assert result.status_code == 200
    assert result.headers["content-disposition"] == 'attachment; filename="clip.mp4"'


def test_bad_ticket_is_403():
    with pytest.raises(HTTPException) as ei:
        asyncio.new_event_loop().run_until_complete(rr.export_clip("nope"))
    assert ei.value.status_code == 403
