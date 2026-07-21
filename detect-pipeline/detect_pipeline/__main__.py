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
  onnx  YOLOv8/YOLO11 ONNX via cv2.dnn (needs --model) — production default
  blob  largest bright blob per region — deterministic, for a quick chain check
  stub  no detections — see motion + regions only
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys

import cv2

from .detector import StubDetector, to_bgr
from .detectors_local import BrightBlobDetector, HogPersonDetector
from .ffmpeg_presets import HwAccel
from .frame_source import FrameSource, VideoFileSource, probe_stream
from .motion import MotionConfig, MotionDetector
from .pipeline import DetectPipeline
from .tracking import TrackConfig, Tracker

_YELLOW = (0, 220, 220)
_BLUE = (255, 160, 0)
_GREEN = (60, 220, 60)


def _make_detector(args):
    if args.detector == "onnx":
        if not args.model:
            raise SystemExit("--detector onnx requires --model path/to/model.onnx")
        from .onnx_detector import OnnxYoloDetector
        return OnnxYoloDetector(model_path=args.model, input_size=args.onnx_input)
    if args.detector == "hog":
        return HogPersonDetector()
    if args.detector == "blob":
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


def _open_source(args) -> tuple[object, int, int]:
    """Return (frame iterator, width, height). RTSP goes through the production
    ffmpeg FrameSource (hwaccel, tcp); files through OpenCV VideoFileSource."""
    if args.source.startswith(("rtsp://", "rtsps://")):
        w, h, fps = args.width, args.height, args.fps
        if not (w and h):
            probed = probe_stream(args.source, rtsp_transport=args.rtsp_transport)
            if probed is None:
                raise SystemExit(
                    "could not probe stream resolution — pass --width and --height "
                    "(and check the URL / credentials / TLS)"
                )
            w, h, _fps = probed
        # A live stream is infinite: bound it so a test run terminates.
        max_frames = args.max_frames or (args.seconds * args.fps if args.seconds else args.fps * 20)
        src = FrameSource(
            args.source, width=w, height=h, fps=args.fps,
            hwaccel=HwAccel(args.hwaccel), device=args.device,
            rtsp_transport=args.rtsp_transport, max_restarts=0,
        )
        return itertools.islice(src.stream(), max_frames), w, h

    # file: peek the first frame for resolution, then chain it back
    src = VideoFileSource(args.source, max_frames=args.max_frames)
    stream = src.stream()
    first = next(stream)
    return itertools.chain([first], stream), first.width, first.height


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        frames_iter, w, h = _open_source(args)
    except (SystemExit, RuntimeError, StopIteration) as e:
        print(f"no frames from source: {e}", file=sys.stderr)
        return 2

    motion = MotionDetector((h, w), MotionConfig(frame_height=args.motion_height))
    tracker = Tracker((h, w), TrackConfig(fps=args.fps, min_initialized=max(1, args.fps // 2)))
    # process_frame is driven directly here, so the pipeline's own frame_source
    # is unused (None); run() is only for the always-on production loop.
    pipe = DetectPipeline(
        None, motion, _make_detector(args), tracker,
        model_size=(args.model_size, args.model_size),
    )

    writer = None
    if args.out:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(args.out, fourcc, float(args.fps), (w, h))

    frames = 0
    ids: set[int] = set()
    max_concurrent = 0

    for frame in frames_iter:
        result = pipe.process_frame(frame)
        frames += 1
        ids.update(t.id for t in result.tracks)
        max_concurrent = max(max_concurrent, len(result.tracks))
        if writer is not None:
            bgr = to_bgr(frame.data, frame.width, frame.height)
            _draw(bgr, result)
            writer.write(bgr)

    if writer is not None:
        writer.release()

    if frames == 0:
        print("no frames processed", file=sys.stderr)
        return 2

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
    p.add_argument("--detector", choices=["onnx", "hog", "blob", "stub"], default="hog")
    p.add_argument("--model", default=None, help="ONNX model path (for --detector onnx)")
    p.add_argument("--onnx-input", type=int, default=640, help="ONNX model input size")
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--model-size", type=int, default=320)
    p.add_argument("--motion-height", type=int, default=180)
    p.add_argument("--max-frames", type=int, default=None)
    # RTSP-source options (ignored for file sources)
    p.add_argument("--hwaccel", choices=[a.value for a in HwAccel], default="cpu",
                   help="hardware decode backend for rtsp sources")
    p.add_argument("--device", default="/dev/dri/renderD128", help="hwaccel device")
    p.add_argument("--rtsp-transport", default="tcp", choices=["tcp", "udp"])
    p.add_argument("--width", type=int, default=None, help="override probed width")
    p.add_argument("--height", type=int, default=None, help="override probed height")
    p.add_argument("--seconds", type=int, default=None,
                   help="stop a live rtsp stream after N seconds (default 20)")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
