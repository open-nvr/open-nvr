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

import logging

import cv2
import numpy as np

from .metrics import metrics
from .regions import Box

log = logging.getLogger("detect_pipeline.detector")
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


class DetectorPool:
    """A detector shared by many camera workers, backed by a bounded pool.

    One detector instance PER CAMERA is the memory wall of this service: each
    holds its own copy of the model weights plus its own activation arenas,
    so a fleet pays N x that before it runs out of anything else. But the
    instances cannot simply be shared — ``cv2.dnn.Net.forward`` is not safe to
    call concurrently on one Net, which is why the manager built one per
    worker in the first place.

    A pool keeps both properties: a detector is still used by exactly one
    thread at a time (borrowed for the duration of a single ``detect`` call),
    while the number of instances is capped independently of the camera count.

    Grown lazily, so a small install never allocates more than it uses: with
    fewer cameras than ``max_size`` this behaves exactly as before, one
    detector each. Throughput is unaffected at sane sizes — inference is
    CPU-bound and releases the GIL, so more concurrent detectors than cores
    buys nothing but memory.
    """

    def __init__(self, factory, max_size: int) -> None:
        import threading

        self._factory = factory
        self.max_size = max(1, int(max_size))
        self._free: list = []
        self._created = 0
        self._degraded = 0
        self._reject_type = StubDetector
        self._lock = threading.Lock()
        # Admission control: never more concurrent detects than instances.
        self._slots = threading.Semaphore(self.max_size)

    def _take(self):
        with self._lock:
            if self._free:
                return self._free.pop()
        det = self._factory()           # built outside the lock: model load is slow
        # Count only what was actually BUILT. Incrementing before the factory
        # ran meant a failed load inflated the count for good, quietly shrinking
        # real capacity below max_size with no signal.
        with self._lock:
            self._created += 1
        return det

    def _usable(self, det) -> bool:
        """Whether an instance is worth returning to the pool.

        The factory degrades to a StubDetector when a model fails to load. Under
        one-detector-per-worker that blinded exactly one camera; in a SHARED pool
        the stub would circulate forever, so a rotating subset of frames across
        every camera would silently detect nothing — and /health could not see
        it, because it samples a separate probe instance built once at startup.
        """
        if self._reject_type is None:
            return True
        return not isinstance(det, self._reject_type)

    def _give(self, det) -> None:
        if not self._usable(det):
            with self._lock:
                self._created = max(0, self._created - 1)
                self._degraded += 1
            log.warning(
                "detector pool: discarding a degraded (%s) instance instead of "
                "recirculating it — it would blind a share of every camera's "
                "frames. Rebuilt on the next borrow.",
                type(det).__name__,
            )
            metrics.inc("tier0_detector_pool_degraded_total")
            return
        with self._lock:
            self._free.append(det)

    def detect(self, crop):
        self._slots.acquire()
        try:
            det = self._take()
            try:
                return det.detect(crop)
            finally:
                self._give(det)
        finally:
            self._slots.release()

    @property
    def created(self) -> int:
        """Detector instances actually built (<= max_size)."""
        with self._lock:
            return self._created
