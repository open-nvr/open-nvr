# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end smoke test: synthetic clip → real pipeline → tracks + annotated MP4.

This is the automated form of the manual verification: it drives the ACTUAL
pipeline (motion → regions → detector → tracker) over a generated video and
proves a moving object is detected and tracked, and that the CLI writes output.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from detect_pipeline.__main__ import main
from detect_pipeline.detectors_local import BrightBlobDetector
from detect_pipeline.frame_source import VideoFileSource
from detect_pipeline.motion import MotionConfig, MotionDetector
from detect_pipeline.pipeline import DetectPipeline
from detect_pipeline.tracking import TrackConfig, Tracker

W, H, N = 320, 240, 60


def _make_clip(path: str) -> None:
    """Static gray for a while (motion calibrates), then a bright square moves."""
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (W, H))
    if not writer.isOpened():
        pytest.skip("no MP4 writer backend available in this environment")
    for i in range(N):
        frame = np.full((H, W, 3), 110, np.uint8)
        if i >= 25:                                   # after calibration
            x = 20 + (i - 25) * 8
            frame[100:150, x : x + 50] = 255          # moving bright square
        writer.write(frame)
    writer.release()


@pytest.fixture()
def clip(tmp_path):
    p = str(tmp_path / "synthetic.mp4")
    _make_clip(p)
    cap = cv2.VideoCapture(p)
    ok = cap.isOpened() and cap.read()[0]
    cap.release()
    if not ok:
        pytest.skip("MP4 read-back not supported in this environment")
    return p


def test_pipeline_detects_and_tracks_moving_object(clip):
    src = VideoFileSource(clip)
    motion = MotionDetector((H, W), MotionConfig(frame_height=120))
    tracker = Tracker((H, W), TrackConfig(fps=10, min_initialized=2))
    pipe = DetectPipeline(src, motion, BrightBlobDetector(), tracker, model_size=(320, 320))

    seen_track = False
    for frame in src.stream():
        result = pipe.process_frame(frame)
        if result.tracks:
            seen_track = True
    assert seen_track, "a moving bright square should be detected and tracked"


def test_cli_writes_annotated_output(clip, tmp_path):
    out = str(tmp_path / "annotated.mp4")
    rc = main(["--source", clip, "--out", out, "--detector", "blob", "--fps", "10",
               "--motion-height", "120"])
    assert rc == 0
    cap = cv2.VideoCapture(out)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    assert n > 0                                      # annotated video has frames
