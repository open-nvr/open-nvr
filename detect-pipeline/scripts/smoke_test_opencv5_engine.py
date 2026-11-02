#!/usr/bin/env python3
"""Smoke-test real YOLOv8n inference using OpenCV 5 ENGINE_NEW."""

import argparse
from pathlib import Path

import cv2

from detect_pipeline.onnx_detector import OnnxYoloDetector


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test YOLOv8n ONNX inference with OpenCV 5 ENGINE_NEW."
    )
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--input-size", type=int, default=640)
    parser.add_argument("--conf-threshold", type=float, default=0.25)
    parser.add_argument("--iou-threshold", type=float, default=0.45)
    args = parser.parse_args()

    if not args.model.is_file():
        parser.error(f"Model not found: {args.model}")

    if not args.image.is_file():
        parser.error(f"Image not found: {args.image}")

    if int(cv2.__version__.split(".")[0]) < 5:
        print(f"ERROR: OpenCV 5.x required, found {cv2.__version__}")
        return 1

    if not hasattr(cv2.dnn, "ENGINE_NEW"):
        print("ERROR: ENGINE_NEW is unavailable.")
        return 1

    image = cv2.imread(str(args.image))
    if image is None:
        print(f"ERROR: Could not read image: {args.image}")
        return 1

    print(f"OpenCV version: {cv2.__version__}")
    print("Loading model with ENGINE_NEW...")

    try:
        net = cv2.dnn.readNetFromONNX(
            str(args.model),
            cv2.dnn.ENGINE_NEW,
        )

        detector = OnnxYoloDetector(
            net=net,
            input_size=args.input_size,
            conf_threshold=args.conf_threshold,
            iou_threshold=args.iou_threshold,
        )

        detections = detector.detect(image)

    except Exception as exc:
        print(f"ERROR: Inference failed: {exc}")
        return 1

    if not detections:
        print("ERROR: No detections returned.")
        return 1

    labels = {d.label for d in detections}

    if not {"person", "bus"}.issubset(labels):
        print(f"ERROR: Expected person and bus; got {sorted(labels)}")
        return 1

    for detection in detections:
        x1, y1, x2, y2 = detection.box

        if not all(0.0 <= value <= 1.0 for value in detection.box):
            print(f"ERROR: Invalid normalized box: {detection.box}")
            return 1

        if x1 > x2 or y1 > y2:
            print(f"ERROR: Invalid box ordering: {detection.box}")
            return 1

        print(
            f"{detection.label}: "
            f"confidence={detection.score:.4f}, "
            f"box=({x1:.4f}, {y1:.4f}, {x2:.4f}, {y2:.4f})"
        )

    print(f"PASS: ENGINE_NEW produced {len(detections)} detections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
