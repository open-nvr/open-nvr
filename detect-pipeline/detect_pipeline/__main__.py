# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Manual-verification CLI for the Tier-0 detect pipeline.

Run the real pipeline on a video file (or RTSP URL) and write an annotated MP4
you can eyeball — motion boxes (yellow), detector regions (blue), tracked objects
with IDs (green), and a CALIBRATING banner while the motion detector warms up.

    python -m detect_pipeline --source people.mp4 --out annotated.mp4 --detector hog
    python -m detect_pipeline --source rtsp://127.0.0.1:8554/cam_sub --out out.mp4 --detector hog

Detectors:
  hog   OpenCV pedestrian detector — real people, no model download (default)
  blob  largest bright blob per region — deterministic, for a quick chain check
  stub  no detections — see motion + regions only
"""
from __future__ import annotations

import argparse
import logging
import sys

import cv2
import numpy as np

from .detector import StubDetector, to_bgr
from .detectors_local import BrightBlobDetector, HogPersonDetector
from .frame_source import VideoFileSource
from .motion import MotionConfig, MotionDetector
from .pipeline import DetectPipeline
from .tracking import TrackConfig, Tracker

_YELLOW = (0, 220, 220)
_BLUE = (255, 160, 0)
_GREEN = (60, 220, 60)


def _make_detector(name: str):
    if name == "hog":
        return HogPersonDetector()
    if name == "blob":
        return BrightBlobDetector()
    return StubDetector()


def _draw(bgr, result) -> None:
    for b in result.regions:
        cv2.rectangle(bgr, (b[0], b[1]), (b[2], b[3]), _BLUE, 1)
    for b in result.motion_boxes:
        cv2.rectangle(bgr, (b[0], b[1]), (b[2], b[3]), _YELLOW, 1)
    for t in result.tracks:
        x1, y1, x2, y2 = t.box
        cv2.rectangle(bgr, (x1, y1), (x2, y2), _GREEN, 2)
        tag = f"#{t.id} {t.label} {t.score:.2f}" + (" [stationary]" if t.stationary else "")
        cv2.putText(bgr, tag, (x1, max(15, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, _GREEN, 1)
    if result.calibrating:
        cv2.putText(bgr, "CALIBRATING", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    source = VideoFileSource(args.source, max_frames=args.max_frames)

    # Peek the first frame to learn the resolution, then rebuild the stream.
    stream = source.stream()
    try:
        first = next(stream)
    except StopIteration:
        print("no frames read from source", file=sys.stderr)
        return 2
    h, w = first.height, first.width

    motion = MotionDetector((h, w), MotionConfig(frame_height=args.motion_height))
    tracker = Tracker((h, w), TrackConfig(fps=args.fps, min_initialized=max(1, args.fps // 2)))
    pipe = DetectPipeline(
        source, motion, _make_detector(args.detector), tracker,
        model_size=(args.model_size, args.model_size),
    )

    writer = None
    if args.out:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(args.out, fourcc, float(args.fps), (w, h))

    frames = 0
    ids: set[int] = set()
    max_concurrent = 0

    def handle(frame):
        nonlocal frames, max_concurrent
        result = pipe.process_frame(frame)
        frames += 1
        ids.update(t.id for t in result.tracks)
        max_concurrent = max(max_concurrent, len(result.tracks))
        if writer is not None:
            bgr = to_bgr(frame.data, frame.width, frame.height)
            _draw(bgr, result)
            writer.write(bgr)

    handle(first)                      # the frame we already pulled
    for frame in stream:               # the rest
        handle(frame)

    if writer is not None:
        writer.release()

    print(
        f"processed {frames} frames @ {w}x{h} | detector={args.detector} | "
        f"unique tracks={len(ids)} | max concurrent={max_concurrent}"
        + (f" | wrote {args.out}" if args.out else "")
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="detect_pipeline", description=__doc__)
    p.add_argument("--source", required=True, help="video file path or rtsp:// URL")
    p.add_argument("--out", default=None, help="annotated MP4 to write")
    p.add_argument("--detector", choices=["hog", "blob", "stub"], default="hog")
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--model-size", type=int, default=320)
    p.add_argument("--motion-height", type=int, default=180)
    p.add_argument("--max-frames", type=int, default=None)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
