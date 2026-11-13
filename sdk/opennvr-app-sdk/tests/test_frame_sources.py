# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Frame-source tests — the file / http snapshot sources, the scheme
factory, and the dict bridge into the FrameApp poll loop. Promoted
alongside the module from the package-delivery example."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from opennvr_app_sdk import frame_sources as fs
from opennvr_app_sdk.frame_sources import (
    DictFrameSource,
    FileFrameSource,
    FrameSourceError,
    HttpSnapshotSource,
    build_frame_source,
)


# ── FileFrameSource ────────────────────────────────────────────────


def test_file_source_round_trips_bytes(tmp_path):
    p = tmp_path / "frame.jpg"
    p.write_bytes(b"\xff\xd8jpegbytes")
    source = FileFrameSource(camera_id="cam-1", path=str(p))
    assert source.camera_id == "cam-1"
    assert source.fetch() == b"\xff\xd8jpegbytes"


def test_file_source_rejects_missing_file(tmp_path):
    with pytest.raises(FrameSourceError, match="does not exist"):
        FileFrameSource(camera_id="cam-1", path=str(tmp_path / "nope.jpg"))


# ── HttpSnapshotSource ─────────────────────────────────────────────


class _FakeGet:
    def __init__(self, status_code=200, content=b"img", content_type="image/jpeg",
                 raise_exc: Exception | None = None):
        self.calls: list[str] = []
        self._response = SimpleNamespace(
            status_code=status_code,
            content=content,
            headers={"content-type": content_type},
        )
        self._raise = raise_exc

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        if self._raise is not None:
            raise self._raise
        return self._response


def test_http_source_returns_body_on_200(monkeypatch):
    fake = _FakeGet(content=b"snapshot-bytes")
    monkeypatch.setattr(fs.httpx, "get", fake)
    source = HttpSnapshotSource(camera_id="cam-1", url="http://cam/snap.jpg")
    assert source.fetch() == b"snapshot-bytes"
    assert fake.calls == ["http://cam/snap.jpg"]


def test_http_source_wraps_transport_errors(monkeypatch):
    monkeypatch.setattr(fs.httpx, "get", _FakeGet(raise_exc=OSError("boom")))
    source = HttpSnapshotSource(camera_id="cam-1", url="http://cam/snap.jpg")
    with pytest.raises(FrameSourceError, match="OSError"):
        source.fetch()


def test_http_source_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(fs.httpx, "get", _FakeGet(status_code=503))
    source = HttpSnapshotSource(camera_id="cam-1", url="http://cam/snap.jpg")
    with pytest.raises(FrameSourceError, match="HTTP 503"):
        source.fetch()


def test_http_source_warns_on_non_image_content_type(monkeypatch, caplog):
    fake = _FakeGet(content=b"<html>", content_type="text/html; charset=utf-8")
    monkeypatch.setattr(fs.httpx, "get", fake)
    source = HttpSnapshotSource(camera_id="cam-1", url="http://cam/snap.jpg")
    with caplog.at_level("WARNING", logger="opennvr_app_sdk.frame_sources"):
        assert source.fetch() == b"<html>"  # passed through anyway
    assert any("non-image" in r.getMessage() for r in caplog.records)


def test_http_source_rejects_non_http_url():
    with pytest.raises(FrameSourceError, match="expected http"):
        HttpSnapshotSource(camera_id="cam-1", url="ftp://cam/snap.jpg")


# ── build_frame_source ─────────────────────────────────────────────


def test_factory_routes_file_scheme(tmp_path):
    p = tmp_path / "frame.jpg"
    p.write_bytes(b"x")
    source = build_frame_source(camera_id="cam-1", url=f"file://{p}")
    assert isinstance(source, FileFrameSource)


def test_factory_routes_http_schemes():
    for url in ("http://cam/snap.jpg", "https://cam/snap.jpg"):
        source = build_frame_source(camera_id="cam-1", url=url)
        assert isinstance(source, HttpSnapshotSource)


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("opennvr://cameras/1/snapshot", "opennvr"),
        ("gopher://cam/snap", "unsupported frame source scheme"),
    ],
)
def test_factory_rejects_unsupported_schemes(url, match):
    with pytest.raises(FrameSourceError, match=match):
        build_frame_source(camera_id="cam-1", url=url)


def test_factory_routes_rtsp_to_the_stream_backed_source():
    """rtsp:// used to be refused outright. It now keeps one decoder
    warm and hands back its newest frame, so a polling FrameApp can be
    pointed at a stream without paying an ffmpeg start per tick."""
    from opennvr_app_sdk.rtsp import RtspStillSource

    source = build_frame_source(camera_id="cam-1", url="rtsp://cam/stream")
    assert isinstance(source, RtspStillSource)


# ── DictFrameSource bridge ─────────────────────────────────────────


class _StubSource:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def fetch(self) -> bytes:
        return self.payload


def test_dict_source_routes_by_camera_id():
    sources = {"cam-1": _StubSource(b"one"), "cam-2": _StubSource(b"two")}
    bridge = DictFrameSource(sources)
    assert bridge.get_frame("cam-1") == b"one"
    assert bridge.get_frame("cam-2") == b"two"


def test_dict_source_sees_live_mapping_swaps():
    sources = {"cam-1": _StubSource(b"before")}
    bridge = DictFrameSource(sources)
    assert bridge.get_frame("cam-1") == b"before"
    sources["cam-1"] = _StubSource(b"after")  # test-stub swap pattern
    assert bridge.get_frame("cam-1") == b"after"


def test_dict_source_unknown_camera_raises_key_error():
    with pytest.raises(KeyError):
        DictFrameSource({}).get_frame("cam-x")
