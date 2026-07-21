# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Detector-adapter interface + tensor shaping for the Tier-0 pipeline.

This is the seam to KAI-C. The pipeline never calls a model directly — it hands a
cropped, resized region to a ``DetectorAdapter`` and gets back normalized boxes.
The framework (this module), like Frigate's ``create_tensor_input``, owns the
pixel shaping (YUV→BGR, crop, resize) so an adapter stays layout-simple and only
returns ``(label, score, normalized box)`` — the network-message form of Frigate's
``(K,6)`` contract.

For PR A the concrete adapter is a local stub / reference; the KAI-C HTTP/WS
client that dispatches the crop to a governed accelerator adapter and parses the
response lands with the contract detector spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from .regions import Box
from .tracking import Detection


@dataclass(frozen=True)
class RawDetection:
    """A detector result normalized to the crop it ran on.

    ``box`` is ``(x1, y1, x2, y2)`` in 0–1 fractions of the crop.
    """

    label: str
    score: float
    box: tuple[float, float, float, float]


class DetectorAdapter(Protocol):
    """A cheap object detector. Implemented locally (stub) or via a KAI-C-backed
    HTTP/WS adapter running on an accelerator."""

    def detect(self, crop: np.ndarray) -> list[RawDetection]:
        ...


def to_bgr(frame_data: bytes, width: int, height: int) -> np.ndarray:
    """Convert one raw I420 (yuv420p) frame to an (H, W, 3) BGR array.

    Done once per frame; every region crop is taken from this, so per-region
    colour conversion is avoided.
    """
    yuv = np.frombuffer(frame_data, np.uint8).reshape(height * 3 // 2, width)
    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)


def crop_and_resize(bgr: np.ndarray, region: Box, out_w: int, out_h: int) -> np.ndarray:
    """Crop ``region`` from a full-frame BGR image and resize to the model input."""
    x1, y1, x2, y2 = region
    crop = bgr[y1:y2, x1:x2]
    if crop.size == 0:
        raise ValueError(f"empty crop for region {region}")
    return cv2.resize(crop, (out_w, out_h), interpolation=cv2.INTER_LINEAR)


def detections_to_frame(raws: list[RawDetection], region: Box) -> list[Detection]:
    """Map crop-normalized detections back to full-frame pixel coordinates."""
    rx1, ry1, rx2, ry2 = region
    rw, rh = (rx2 - rx1), (ry2 - ry1)
    out: list[Detection] = []
    for r in raws:
        bx1, by1, bx2, by2 = r.box
        out.append(
            Detection(
                label=r.label,
                box=(
                    int(rx1 + bx1 * rw),
                    int(ry1 + by1 * rh),
                    int(rx1 + bx2 * rw),
                    int(ry1 + by2 * rh),
                ),
                score=r.score,
            )
        )
    return out


class StubDetector:
    """A no-op detector (reference impl / test double). Returns nothing."""

    def detect(self, crop: np.ndarray) -> list[RawDetection]:
        return []
