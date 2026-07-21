# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ONNX object detector via ``cv2.dnn`` — the default Tier-0 reference detector.

Loads a YOLOv8/YOLO11-family ONNX model with ``cv2.dnn.readNetFromONNX`` (present
in the OpenCV *main* wheel on both 4.x and 5.x — unlike HOG, which OpenCV 5 moved
to opencv_contrib). This is the same ONNX path the KAI-C accelerator adapter and
the stack's ``yolov8n.onnx`` use, so the local detector is consistent with the
production one and far better quality than HOG.

The decode (``postprocess_yolo``) is a pure function tested against synthetic
output tensors, and the model/network is injectable, so nothing here requires a
model file or a download at test time — the real model only loads in deployment.

YOLOv8/YOLO11 detect output is ``(1, 4+nc, N)`` (e.g. ``(1, 84, 8400)`` for COCO):
4 box params (cx, cy, w, h in input pixels) + ``nc`` class scores per anchor, no
objectness, no softmax. Transpose to ``(N, 4+nc)``, argmax the class scores,
threshold, then ``cv2.dnn.NMSBoxes``.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from .detector import RawDetection

log = logging.getLogger("detect_pipeline.onnx_detector")

# COCO-80 class names (the classes the default yolov8n.onnx predicts).
COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def postprocess_yolo(
    output: np.ndarray,
    *,
    input_size: int,
    conf_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    labels: list[str] | None = None,
) -> list[RawDetection]:
    """Decode a YOLOv8/YOLO11 ONNX output tensor into normalized detections.

    ``output`` may be ``(1, 4+nc, N)``, ``(4+nc, N)``, or ``(N, 4+nc)``. Boxes are
    returned normalized to ``[0, 1]`` of the model input (which equals the crop).
    """
    labels = labels or COCO_LABELS
    arr = np.squeeze(np.asarray(output))
    if arr.ndim != 2:
        return []
    # Orient to (N, 4+nc): detections are the long axis.
    if arr.shape[0] < arr.shape[1]:
        arr = arr.T
    if arr.shape[1] < 5:
        return []

    boxes_xywh = arr[:, :4].astype(np.float32)
    class_scores = arr[:, 4:]
    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(class_scores.shape[0]), class_ids]

    keep = confidences >= conf_threshold
    if not np.any(keep):
        return []
    boxes_xywh, class_ids, confidences = boxes_xywh[keep], class_ids[keep], confidences[keep]

    cx, cy, w, h = boxes_xywh[:, 0], boxes_xywh[:, 1], boxes_xywh[:, 2], boxes_xywh[:, 3]
    x1, y1 = cx - w / 2.0, cy - h / 2.0
    rects = np.stack([x1, y1, w, h], axis=1).tolist()  # NMSBoxes wants [x, y, w, h]

    idxs = cv2.dnn.NMSBoxes(rects, confidences.tolist(), conf_threshold, iou_threshold)
    if idxs is None or len(idxs) == 0:
        return []

    out: list[RawDetection] = []
    for i in np.asarray(idxs).reshape(-1):
        cid = int(class_ids[i])
        label = labels[cid] if 0 <= cid < len(labels) else str(cid)
        out.append(
            RawDetection(
                label,
                float(confidences[i]),
                (
                    _clamp01(x1[i] / input_size),
                    _clamp01(y1[i] / input_size),
                    _clamp01((x1[i] + w[i]) / input_size),
                    _clamp01((y1[i] + h[i]) / input_size),
                ),
            )
        )
    return out


class OnnxYoloDetector:
    """YOLOv8/YOLO11 ONNX detector run through cv2.dnn (CPU)."""

    def __init__(
        self,
        model_path: str | None = None,
        *,
        input_size: int = 640,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        labels: list[str] | None = None,
        net=None,  # injectable for tests (obj with .setInput()/.forward())
    ) -> None:
        self.input_size = input_size
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.labels = labels or COCO_LABELS
        if net is not None:
            self._net = net
        else:
            if not model_path:
                raise ValueError("OnnxYoloDetector needs a model_path or an injected net")
            self._net = cv2.dnn.readNetFromONNX(model_path)

    def detect(self, crop: np.ndarray) -> list[RawDetection]:
        blob = cv2.dnn.blobFromImage(
            crop, scalefactor=1 / 255.0,
            size=(self.input_size, self.input_size), swapRB=True, crop=False,
        )
        self._net.setInput(blob)
        output = self._net.forward()
        return postprocess_yolo(
            output, input_size=self.input_size,
            conf_threshold=self.conf_threshold, iou_threshold=self.iou_threshold,
            labels=self.labels,
        )
