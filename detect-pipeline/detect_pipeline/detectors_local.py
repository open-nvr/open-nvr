# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Local reference detectors for manual verification (no model download, no KAI-C).

* ``HogPersonDetector`` — OpenCV's built-in HOG + SVM pedestrian detector. Real
  person detection with zero extra assets, so you can point the pipeline at a
  video of people walking and *see* boxes + tracking.
* ``BrightBlobDetector`` — deterministic: the largest bright blob in a crop.
  Used by the automated smoke test (a synthetic moving square) and handy for a
  quick "is the whole chain alive?" check on any clip.

These stand in for a real KAI-C-backed accelerator adapter until that lands.
"""
from __future__ import annotations

import cv2
import numpy as np

from .detector import RawDetection


class HogPersonDetector:
    """OpenCV HOG pedestrian detector wrapped as a DetectorAdapter."""

    def __init__(self, win_stride: int = 8, scale: float = 1.05) -> None:
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self._win_stride = (win_stride, win_stride)
        self._scale = scale

    def detect(self, crop: np.ndarray) -> list[RawDetection]:
        h, w = crop.shape[:2]
        rects, weights = self._hog.detectMultiScale(
            crop, winStride=self._win_stride, padding=(8, 8), scale=self._scale
        )
        out: list[RawDetection] = []
        for (x, y, rw, rh), score in zip(rects, np.ravel(weights)):
            out.append(
                RawDetection(
                    "person",
                    float(score),
                    (
                        max(0.0, x / w),
                        max(0.0, y / h),
                        min(1.0, (x + rw) / w),
                        min(1.0, (y + rh) / h),
                    ),
                )
            )
        return out


class BrightBlobDetector:
    """Detects bright blobs in a crop. Deterministic; for self-tests / smoke runs."""

    def __init__(self, threshold: int = 200, min_area_frac: float = 0.01, label: str = "object") -> None:
        self.threshold = threshold
        self.min_area_frac = min_area_frac
        self.label = label

    def detect(self, crop: np.ndarray) -> list[RawDetection]:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _, th = cv2.threshold(gray, self.threshold, 255, cv2.THRESH_BINARY)
        found = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = found[0] if len(found) == 2 else found[1]
        h, w = gray.shape
        min_area = self.min_area_frac * w * h
        out: list[RawDetection] = []
        for c in contours:
            if cv2.contourArea(c) < min_area:
                continue
            x, y, rw, rh = cv2.boundingRect(c)
            score = min(1.0, 0.5 + cv2.contourArea(c) / (w * h))
            out.append(
                RawDetection(
                    self.label,
                    score,
                    (x / w, y / h, (x + rw) / w, (y + rh) / h),
                )
            )
        return out
