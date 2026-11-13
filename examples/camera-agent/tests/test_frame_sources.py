# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Frame-source tests, including credential hygiene in error/log paths.

ffmpeg echoes the input RTSP URL (with embedded user:pass) in its stderr,
so RtspFrameSource must scrub credentials before they reach an error
message or the logs. subprocess is monkeypatched so these are deterministic
and don't touch the network.
"""
from __future__ import annotations

import subprocess
import tempfile

import pytest

import frame_sources as fs


# ── factory + redaction helpers ───────────────────────────────────────


def test_build_frame_source_rtsp_returns_rtsp_source():
    src = fs.build_frame_source(camera_id="c1", url="rtsp://h:554/s")
    assert type(src).__name__ == "RtspFrameSource"


def test_file_frame_source_reads_bytes():
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
        fh.write(b"\xff\xd8JPEG")
        path = fh.name
    src = fs.build_frame_source(camera_id="c1", url="file://" + path)
    assert src.fetch() == b"\xff\xd8JPEG"


def test_file_frame_source_accepts_a_standard_file_uri(tmp_path):
    # file:///C:/… on Windows, file:///tmp/… elsewhere.
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8JPEG")
    src = fs.build_frame_source(camera_id="c1", url=frame.as_uri())
    assert src.fetch() == b"\xff\xd8JPEG"


def test_redact_strips_credentials_and_jwt_query():
    assert fs._redact("rtsp://u:p@h:554/s") == "rtsp://h:554/s"
    assert fs._redact("rtsp://mediamtx:8554/cam-1?jwt=SECRET") == "rtsp://mediamtx:8554/cam-1?REDACTED"
    assert fs._redact("rtsp://h:554/s") == "rtsp://h:554/s"  # nothing to strip


def test_scrub_creds_removes_userinfo_anywhere():
    assert fs._scrub_creds("open rtsp://user:pass@h/s: failed") == "open rtsp://h/s: failed"
    assert fs._scrub_creds("no creds here") == "no creds here"


# ── the leak regression: ffmpeg stderr must be scrubbed ───────────────


def test_rtsp_grab_requests_a_keyframe(monkeypatch):
    """Regression: long-GOP H.265 cameras (CPplus) returned a grey wash
    because ffmpeg handed back the first *decodable* frame, which mid-GOP is
    a reference-less P/B-frame. The grab must ask for a keyframe via
    `-skip_frame nokey`, and it must be an INPUT option (before `-i`) so it
    applies to the decoder, not the (nonexistent) output encoder."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, capture_output=True, timeout=None):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"\xff\xd8jpeg", stderr=b"")

    monkeypatch.setattr(fs.subprocess, "run", fake_run)
    src = fs.RtspFrameSource(camera_id="cam1", url="rtsp://h:554/cam-1", timeout_seconds=1)
    assert src.fetch() == b"\xff\xd8jpeg"

    cmd = captured["cmd"]
    assert "-skip_frame" in cmd
    assert cmd[cmd.index("-skip_frame") + 1] == "nokey"
    # must come before -i (input/decoder option), else it's a no-op
    assert cmd.index("-skip_frame") < cmd.index("-i")


def test_rtsp_error_message_does_not_leak_credentials(monkeypatch):
    url = "rtsp://admin:hunter2@10.0.0.9:554/Streaming/Channels/101"

    def fake_run(cmd, capture_output=True, timeout=None):
        # Mimic ffmpeg failing and echoing the full URL (with creds) in stderr.
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout=b"",
            stderr=(f"[rtsp] {url}: 401 Unauthorized").encode(),
        )

    monkeypatch.setattr(fs.subprocess, "run", fake_run)
    src = fs.RtspFrameSource(camera_id="cam9", url=url, timeout_seconds=1)
    with pytest.raises(fs.FrameSourceError) as ei:
        src.fetch()
    msg = str(ei.value)
    assert "admin:hunter2" not in msg, msg   # credentials must be scrubbed
    assert "cam9" in msg                      # but the camera id stays useful
    assert "401 Unauthorized" in msg          # and the real cause survives
