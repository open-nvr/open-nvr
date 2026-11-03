#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-inference smoke test for OpenCV 5's new DNN engine.

Loads the model the way the service does (``OnnxYoloDetector(model_path=...)``,
which lets OpenCV pick the engine) and reports which engine it got; then loads
the same model with ``ENGINE_NEW`` forced and checks both runs agree. The
expected scene is the Ultralytics ``bus.jpg`` (one bus, several people), so
the checks are about that picture, not just "something came back".

Manual, not CI: CI has no model and downloads nothing. See the README,
"OpenCV 5: real inference through the new DNN engine".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

from detect_pipeline.onnx_detector import CvDnnBackend, OnnxYoloDetector

MIN_PEOPLE = 3            # bus.jpg has four, one half out of frame
MIN_BUS_WIDTH = 0.5       # the bus fills most of the frame width
MIN_BUS_HEIGHT = 0.3
BOX_AGREEMENT = 0.02      # auto-selected vs forced-new engine, per coordinate


def fail(msg: str) -> int:
    print(f"FAIL: {msg}")
    return 1


def engine_of(net) -> str:
    """Which engine a loaded ``cv2.dnn.Net`` runs on.

    OpenCV exposes no getter, but ``Net.dump()`` differs: the new engine
    describes its graph (``model_format: "ONNX"`` / ``main_graph``), the
    classic engine emits Graphviz (``digraph G``). Call it after a forward:
    the classic engine cannot dump before its layers are allocated.
    """
    try:
        head = net.dump()[:200]
    except cv2.error:
        return "classic (dump refused before allocation)"
    if "main_graph" in head or "model_format" in head:
        return "new"
    if head.lstrip().startswith("digraph"):
        return "classic"
    return "unknown"


def check_scene(dets) -> str | None:
    """bus.jpg-specific checks: a big bus, people standing in front of it."""
    buses = [d for d in dets if d.label == "bus"]
    people = [d for d in dets if d.label == "person"]
    if not buses:
        return f"no bus detected; labels={sorted({d.label for d in dets})}"
    if len(people) < MIN_PEOPLE:
        return f"expected at least {MIN_PEOPLE} people, got {len(people)}"
    bx1, by1, bx2, by2 = max(buses, key=lambda d: d.score).box
    if (bx2 - bx1) < MIN_BUS_WIDTH or (by2 - by1) < MIN_BUS_HEIGHT:
        return f"bus box too small for this picture: {bx1:.3f},{by1:.3f}-{bx2:.3f},{by2:.3f}"
    for p in people:
        px1, py1, px2, py2 = p.box
        if (px2 - px1) >= (py2 - py1):
            return f"a person box is wider than tall: {p.box}"
        if px2 < bx1 or px1 > bx2 or py2 < by1 or py1 > by2:
            return f"a person box does not overlap the bus: {p.box}"
    return None


def same_detections(a, b) -> str | None:
    if sorted(d.label for d in a) != sorted(d.label for d in b):
        return f"labels differ: {sorted(d.label for d in a)} vs {sorted(d.label for d in b)}"
    for da, db in zip(sorted(a, key=lambda d: d.box), sorted(b, key=lambda d: d.box)):
        if max(abs(x - y) for x, y in zip(da.box, db.box)) > BOX_AGREEMENT:
            return f"boxes differ by more than {BOX_AGREEMENT}: {da.box} vs {db.box}"
    return None


def show(title: str, dets) -> None:
    print(f"{title}: {len(dets)} detections")
    for d in sorted(dets, key=lambda d: -d.score):
        x1, y1, x2, y2 = d.box
        print(f"  {d.label:<8} {d.score:.3f}  ({x1:.3f}, {y1:.3f}, {x2:.3f}, {y2:.3f})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", type=Path, required=True, help="the ONNX file the stack ships (yolo26n.onnx)")
    parser.add_argument("--image", type=Path, required=True, help="Ultralytics bus.jpg")
    parser.add_argument("--input-size", type=int, default=640)
    parser.add_argument("--conf-threshold", type=float, default=0.25)
    parser.add_argument("--iou-threshold", type=float, default=0.45)
    args = parser.parse_args()

    major = int(cv2.__version__.split(".")[0])
    print(f"OpenCV {cv2.__version__}")
    if major < 5 or not hasattr(cv2.dnn, "ENGINE_NEW"):
        return fail("this needs OpenCV 5.x with cv2.dnn.ENGINE_NEW")
    if not args.model.is_file():
        return fail(f"model not found: {args.model}")
    if not args.image.is_file():
        return fail(f"image not found: {args.image}")
    image = cv2.imread(str(args.image))
    if image is None:
        return fail(f"could not read image: {args.image}")

    # 1. The production path: the service constructs the detector from a path
    #    and lets OpenCV choose the engine (ENGINE_AUTO); the constructor also
    #    probes the input size. Any exception here is the finding — let it
    #    propagate with its traceback.
    detector = OnnxYoloDetector(
        model_path=str(args.model), input_size=args.input_size, backend="cvdnn",
        conf_threshold=args.conf_threshold, iou_threshold=args.iou_threshold,
    )
    auto = detector.detect(image)
    auto_engine = engine_of(detector._backend._net)
    print(f"service load path: backend={detector.backend_name} input={detector.input_size} "
          f"engine={auto_engine}")
    show("auto-selected engine", auto)
    if auto_engine != "new":
        return fail(f"OpenCV {cv2.__version__} did not pick the new engine for this model "
                    f"(got {auto_engine}); the follow-up this script verifies is not closed")
    problem = check_scene(auto)
    if problem:
        return fail(problem)

    # 2. The same model with the new engine forced, through the same detector.
    net = cv2.dnn.readNetFromONNX(str(args.model), cv2.dnn.ENGINE_NEW)
    forced = OnnxYoloDetector(
        backend_impl=CvDnnBackend(net=net), input_size=detector.input_size,
        conf_threshold=args.conf_threshold, iou_threshold=args.iou_threshold,
    ).detect(image)
    show("ENGINE_NEW forced", forced)
    problem = check_scene(forced) or same_detections(auto, forced)
    if problem:
        return fail(problem)

    print("PASS: the shipped model runs on OpenCV 5's new DNN engine through the service's load path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
