# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frame sources: scheme dispatch, file reads, secret redaction."""
import pytest

from frame_sources import (
    FileFrameSource,
    FrameSourceError,
    RtspFrameSource,
    _redact,
    _scrub_creds,
    build_frame_source,
)


def test_file_source_roundtrip(tmp_path):
    p = tmp_path / "f.jpg"
    p.write_bytes(b"JPEGDATA")
    src = build_frame_source(camera_id="camera_1", url=p.as_uri())
    assert isinstance(src, FileFrameSource)
    assert src.fetch() == b"JPEGDATA"


def test_file_source_missing_file(tmp_path):
    with pytest.raises(FrameSourceError, match="does not exist"):
        build_frame_source(camera_id="c", url=(tmp_path / "nope.jpg").as_uri())


def test_rtsp_scheme_dispatch():
    src = build_frame_source(camera_id="c", url="rtsp://mediamtx:8554/cam-1?jwt=x")
    assert isinstance(src, RtspFrameSource)


def test_unsupported_scheme_rejected():
    with pytest.raises(FrameSourceError, match="unsupported"):
        build_frame_source(camera_id="c", url="ftp://nope/frame.jpg")


def test_redact_strips_jwt_query():
    out = _redact("rtsp://mediamtx:8554/cam-1?jwt=SECRET")
    assert "SECRET" not in out and "REDACTED" in out


def test_redact_strips_userinfo():
    out = _redact("rtsp://admin:hunter2@10.0.0.5:554/stream")
    assert "hunter2" not in out


def test_scrub_creds_in_stderr_text():
    assert "hunter2" not in _scrub_creds(
        "Connection to rtsp://admin:hunter2@10.0.0.5:554/stream failed")
