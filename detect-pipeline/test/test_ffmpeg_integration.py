# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests that exercise REAL ffmpeg/ffprobe (skipped if absent).

The unit tests fake the subprocess; these prove the actual decode/scale filter
strings and the frame reader work against a real ffmpeg binary — the production
path (minus the network hop to a live RTSP source, which needs a camera).
"""
from __future__ import annotations

import shutil
import subprocess

import cv2
import numpy as np
import pytest

from detect_pipeline.ffmpeg_presets import HwAccel, frame_size_bytes, scale_filter
from detect_pipeline.frame_source import _parse_ffprobe, probe_stream, read_frames

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
_HAS_FFPROBE = shutil.which("ffprobe") is not None

W, H, N, FPS = 64, 48, 15, 15


def _write_clip(path: str) -> None:
    wr = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), float(FPS), (W, H))
    if not wr.isOpened():
        pytest.skip("no MP4 writer backend")
    for i in range(N):
        f = np.full((H, W, 3), 100, np.uint8)
        f[10:30, (i % 40): (i % 40) + 10] = 255
        wr.write(f)
    wr.release()


def test_parse_ffprobe_json():
    text = '{"streams":[{"width":640,"height":360,"avg_frame_rate":"30000/1001"}]}'
    w, h, fps = _parse_ffprobe(text)
    assert (w, h) == (640, 360)
    assert 29.9 < fps < 30.0


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_real_ffmpeg_cpu_decode_reads_i420(tmp_path):
    vid = str(tmp_path / "in.mp4")
    _write_clip(vid)
    vf = scale_filter(HwAccel.CPU, width=W, height=H, fps=FPS)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", vid, "-vf", vf, "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frames = list(read_frames(proc.stdout, W, H))
    proc.wait()
    assert len(frames) >= N - 2                       # decoded ~all frames
    assert all(len(f.data) == frame_size_bytes(W, H) for f in frames)
    assert frames[0].y_plane[:1] is not None          # luma addressable


@pytest.mark.skipif(not _HAS_FFPROBE, reason="ffprobe not installed")
def test_probe_stream_reads_resolution(tmp_path):
    vid = str(tmp_path / "in.mp4")
    _write_clip(vid)
    probed = probe_stream(vid)                          # works on files too
    assert probed is not None
    w, h, _fps = probed
    assert (w, h) == (W, H)
