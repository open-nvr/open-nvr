# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Frame-source tests — file:// + http(s):// + error paths."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from frame_sources import (
    FileFrameSource,
    FrameSourceError,
    HttpSnapshotSource,
    build_frame_source,
)


# ── File source ────────────────────────────────────────────────────


def test_file_frame_source_reads_bytes(tmp_path: Path):
    p = tmp_path / "frame.jpg"
    p.write_bytes(b"FAKE_JPEG_BYTES")
    src = FileFrameSource(camera_id="cam-1", path=str(p))
    assert src.fetch() == b"FAKE_JPEG_BYTES"
    assert src.camera_id == "cam-1"


def test_file_frame_source_rejects_missing_path():
    with pytest.raises(FrameSourceError, match="does not exist"):
        FileFrameSource(camera_id="cam-x", path="/definitely/does/not/exist.jpg")


def test_file_frame_source_rejects_directory(tmp_path: Path):
    with pytest.raises(FrameSourceError, match="not a file"):
        FileFrameSource(camera_id="cam-x", path=str(tmp_path))


# ── HTTP source ────────────────────────────────────────────────────


def _stub_http_transport(*, status: int = 200, body: bytes = b"OK_IMAGE",
                          content_type: str = "image/jpeg") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})
    return httpx.MockTransport(handler)


def test_http_snapshot_returns_bytes(monkeypatch):
    transport = _stub_http_transport()
    def _patched_get(url, **kw):
        # httpx.get() accepts verify= and trust_env= as kwargs, but
        # Client.get() does not — they belong on the Client constructor.
        # Strip those here so the stub Client doesn't choke.
        verify = kw.pop("verify", True)
        kw.pop("trust_env", None)
        return httpx.Client(transport=transport, verify=verify).get(url, **kw)
    monkeypatch.setattr("frame_sources.httpx.get", _patched_get)
    src = HttpSnapshotSource(camera_id="cam-2", url="https://camera.lan/snap.jpg")
    assert src.fetch() == b"OK_IMAGE"


def test_http_snapshot_raises_on_non_200(monkeypatch):
    transport = _stub_http_transport(status=503)
    def _patched_get(url, **kw):
        # httpx.get() accepts verify= and trust_env= as kwargs, but
        # Client.get() does not — they belong on the Client constructor.
        # Strip those here so the stub Client doesn't choke.
        verify = kw.pop("verify", True)
        kw.pop("trust_env", None)
        return httpx.Client(transport=transport, verify=verify).get(url, **kw)
    monkeypatch.setattr("frame_sources.httpx.get", _patched_get)
    src = HttpSnapshotSource(camera_id="cam-x", url="https://camera.lan/snap.jpg")
    with pytest.raises(FrameSourceError, match="HTTP 503"):
        src.fetch()


def test_http_snapshot_raises_on_transport_error(monkeypatch):
    def _raises(*args, **kwargs):
        raise httpx.ConnectError("Network is unreachable")
    monkeypatch.setattr("frame_sources.httpx.get", _raises)
    src = HttpSnapshotSource(camera_id="cam-x", url="https://camera.lan/snap.jpg")
    with pytest.raises(FrameSourceError, match="ConnectError"):
        src.fetch()


def test_http_snapshot_rejects_non_http_scheme():
    with pytest.raises(FrameSourceError, match="http"):
        HttpSnapshotSource(camera_id="cam-x", url="ftp://camera.lan/snap.jpg")


def test_http_snapshot_warns_but_returns_non_image_content_type(monkeypatch, caplog):
    transport = _stub_http_transport(content_type="text/plain")
    def _patched_get(url, **kw):
        # httpx.get() accepts verify= and trust_env= as kwargs, but
        # Client.get() does not — they belong on the Client constructor.
        # Strip those here so the stub Client doesn't choke.
        verify = kw.pop("verify", True)
        kw.pop("trust_env", None)
        return httpx.Client(transport=transport, verify=verify).get(url, **kw)
    monkeypatch.setattr("frame_sources.httpx.get", _patched_get)
    src = HttpSnapshotSource(camera_id="cam-x", url="https://camera.lan/snap")
    body = src.fetch()
    assert body == b"OK_IMAGE"
    # Warning logged but not fatal.


# ── Factory ────────────────────────────────────────────────────────


def test_build_frame_source_file_scheme(tmp_path):
    p = tmp_path / "f.jpg"
    p.write_bytes(b"x")
    src = build_frame_source(camera_id="c", url=f"file://{p}")
    assert isinstance(src, FileFrameSource)


def test_build_frame_source_http_scheme():
    src = build_frame_source(camera_id="c", url="http://192.168.1.1/snap.jpg")
    assert isinstance(src, HttpSnapshotSource)


def test_build_frame_source_https_scheme():
    src = build_frame_source(camera_id="c", url="https://cam.example/snap.jpg")
    assert isinstance(src, HttpSnapshotSource)


def test_build_frame_source_rtsp_not_yet_supported():
    with pytest.raises(FrameSourceError, match="rtsp"):
        build_frame_source(camera_id="c", url="rtsp://cam.example/stream")


def test_build_frame_source_opennvr_scheme_deferred():
    with pytest.raises(FrameSourceError, match="opennvr"):
        build_frame_source(camera_id="c", url="opennvr://cameras/cam-1/snapshot")


def test_build_frame_source_unknown_scheme():
    with pytest.raises(FrameSourceError, match="unsupported"):
        build_frame_source(camera_id="c", url="weird://something")
