# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Ported from Frigate's frigate/motion/improved_motion.py — MIT licensed,
# Frigate, Inc., reviewed at commit 6f80bcd19 (v0.18-beta). See repo-root NOTICE.
#
# Faithful port with three deliberate changes:
#   * PTZ autotracking branches removed (OpenNVR has no PTZ autotrack yet).
#   * Debug image saving removed.
#   * scipy.ndimage.gaussian_filter -> cv2.GaussianBlur (drops the scipy dep).
# The algorithm — moving-average background subtraction, contrast moving-average,
# the >=10-frame persistence gate, and the lightning/skip thresholds — is kept.
"""
Tier-0 motion detection.

``is_calibrating()`` is the signal the pipeline reads: while calibrating (warm-up,
a lightning/IR flash, or a scene change) the pipeline must NOT send regions to the
object detector, but recording continues regardless. This is what keeps a
whole-frame brightness change from flooding the detector at dawn/dusk.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class MotionConfig:
    """Motion tunables — defaults match Frigate 6f80bcd19.

    All fields are operator-tunable via ``DETECT_MOTION_*`` env vars
    (see ``service.py``) — issue #373 found them hardcoded, which left
    no way to rescue a camera whose scene never calibrated.
    """

    enabled: bool = True                      # False = gate OFF: full-frame motion every frame (detector always runs; costly)
    threshold: int = 30                       # pixel-diff threshold (1–255)
    contour_area: int = 10                    # min contour area to count as motion
    frame_alpha: float = 0.01                 # background running-avg alpha, steady state
    lightning_threshold: float = 0.8          # >this fraction of frame -> recalibrate (don't detect)
    skip_motion_threshold: float | None = None  # >this fraction -> drop frame entirely (off by default)
    improve_contrast: bool = True
    frame_height: int | None = 100            # downscale luma to this tall for motion (None = full)
    #: #373: calibration deadline. Calibration clears only on a "quiet"
    #: frame (<5% motion, <=4 boxes) — a scene with continuous motion
    #: (trees, rain, a busy road) may NEVER produce one, and the gate
    #: then skipped every frame for the life of the worker with no
    #: signal. After this many consecutive calibrating frames the gate
    #: forces itself open (WARN logged): a scene that is always busy is
    #: the scene, and the detector should see it. 0 disables the
    #: deadline (pre-#373 behaviour). Default 150 ≈ 75 s at 2 fps —
    #: real calibration settles in well under 50 frames.
    calibration_max_frames: int = 150


def _grab_contours(found) -> list:
    """Normalize ``cv2.findContours`` return across OpenCV versions."""
    # OpenCV 4/5: (contours, hierarchy); OpenCV 3: (image, contours, hierarchy)
    return found[0] if len(found) == 2 else found[1]


class MotionDetector:
    """Background-subtraction motion detector operating on the luma plane."""

    def __init__(
        self,
        frame_shape: tuple[int, int],
        config: MotionConfig | None = None,
        *,
        blur_radius: int = 1,
        contrast_frame_history: int = 50,
        interpolation: int = cv2.INTER_NEAREST,
        label: str = "",
    ) -> None:
        self.config = config or MotionConfig()
        #: Camera id (or similar) for log attribution — #373's wedge was
        #: invisible partly because nothing could say WHICH camera.
        self.label = label
        self.frame_shape = frame_shape  # (height, width) of the luma input
        # Downscale for speed, but never *up*scale a small input (clamp).
        fh = min(self.config.frame_height or frame_shape[0], frame_shape[0])
        self.resize_factor = frame_shape[0] / fh
        self.motion_frame_size = (fh, fh * frame_shape[1] // frame_shape[0])
        self.avg_frame = np.zeros(self.motion_frame_size, np.float32)
        self.motion_frame_count = 0
        # Gate OFF (#373): never calibrating, and detect() returns a
        # full-frame motion box so the detector runs on every frame.
        self.calibrating = self.config.enabled
        #: #373: consecutive calibrating frames in the CURRENT episode,
        #: and how many times the deadline forced the gate open.
        self.calibrating_frames = 0
        self.forced_calibration_exits = 0
        self._forced_exit_warned = False
        self.blur_radius = blur_radius
        self.interpolation = interpolation
        self.contrast_values = np.zeros((contrast_frame_history, 2), np.uint8)
        self.contrast_values[:, 1:2] = 255
        self.contrast_values_index = 0

    def is_calibrating(self) -> bool:
        return self.calibrating

    def _enforce_calibration_deadline(self) -> None:
        """#373: bound every calibration episode. Called once per frame
        after the calibration flags settle. A scene with continuous
        motion never produces the 'quiet' frame that clears calibration,
        and the gate then skipped EVERY frame for the life of the worker
        — detector never ran, no visits, no plates, and the only
        evidence was a Prometheus counter. After
        ``calibration_max_frames`` consecutive calibrating frames the
        gate forces itself open and says so."""
        if not self.calibrating:
            self.calibrating_frames = 0
            return
        self.calibrating_frames += 1
        limit = self.config.calibration_max_frames
        if limit <= 0 or self.calibrating_frames < limit:
            return
        self.calibrating = False
        self.calibrating_frames = 0
        self.forced_calibration_exits += 1
        msg = (
            "motion gate %s: calibration did not settle after %d frames "
            "— forcing the gate OPEN so detection runs (forced exits so "
            "far: %d). The scene likely has continuous motion; tune "
            "DETECT_MOTION_THRESHOLD / DETECT_MOTION_CONTOUR_AREA (or "
            "DETECT_MOTION_LIGHTNING_THRESHOLD if this repeats), or set "
            "DETECT_MOTION_ENABLED=false to disable the gate for good."
        )
        args = (self.label or "?", limit, self.forced_calibration_exits)
        if self._forced_exit_warned:
            # Repeats mean the scene re-trips calibration continuously —
            # keep the log readable; the counter carries the tally.
            log.debug(msg, *args)
        else:
            self._forced_exit_warned = True
            log.warning(msg, *args)

    def detect(self, luma: np.ndarray) -> list[tuple[int, int, int, int]]:
        """Return motion boxes (x1, y1, x2, y2) in full-frame coordinates."""
        motion_boxes: list[tuple[int, int, int, int]] = []
        if not self.config.enabled:
            # Gate OFF: the whole frame is "in motion" every frame, so
            # region selection scans the full frame and the detector
            # always runs. This is the #373 escape hatch for scenes the
            # gate cannot model — it costs full-frame inference at
            # DETECT_FPS, so it is an explicit operator choice
            # (DETECT_MOTION_ENABLED=false), never a default.
            return [(0, 0, self.frame_shape[1], self.frame_shape[0])]

        gray = luma[0 : self.frame_shape[0], 0 : self.frame_shape[1]]
        resized = cv2.resize(
            gray,
            dsize=(self.motion_frame_size[1], self.motion_frame_size[0]),
            interpolation=self.interpolation,
        )

        # Contrast normalization against a moving average of the last N min/max,
        # so a single bright frame doesn't blow out the contrast.
        if self.config.improve_contrast:
            min_value = np.percentile(resized, 4).astype(np.uint8)
            max_value = np.percentile(resized, 96).astype(np.uint8)
            if min_value < max_value:
                self.contrast_values[self.contrast_values_index] = [min_value, max_value]
                self.contrast_values_index = (
                    self.contrast_values_index + 1
                ) % len(self.contrast_values)
                avg_min, avg_max = np.mean(self.contrast_values, axis=0)
                resized = np.clip(resized, avg_min, avg_max)
                resized = (((resized - avg_min) / (avg_max - avg_min)) * 255).astype(np.uint8)

        k = 2 * self.blur_radius + 1
        resized = cv2.GaussianBlur(resized, (k, k), sigmaX=1)

        frame_delta = cv2.absdiff(resized, cv2.convertScaleAbs(self.avg_frame))
        thresh = cv2.threshold(frame_delta, self.config.threshold, 255, cv2.THRESH_BINARY)[1]
        thresh_dilated = cv2.dilate(thresh, None, iterations=1)
        contours = _grab_contours(
            cv2.findContours(thresh_dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        )

        total_contour_area = 0.0
        for c in contours:
            area = cv2.contourArea(c)
            total_contour_area += area
            if area > (self.config.contour_area or 0):
                x, y, w, h = cv2.boundingRect(c)
                motion_boxes.append(
                    (
                        int(x * self.resize_factor),
                        int(y * self.resize_factor),
                        int((x + w) * self.resize_factor),
                        int((y + h) * self.resize_factor),
                    )
                )

        pct_motion = total_contour_area / (
            self.motion_frame_size[0] * self.motion_frame_size[1]
        )

        # Skip the frame entirely above skip_motion_threshold (off by default).
        if (
            self.config.skip_motion_threshold is not None
            and pct_motion > self.config.skip_motion_threshold
        ):
            self.calibrating = True
            # #373: dropped frames still count toward the deadline — a
            # scene permanently above skip_motion_threshold must not
            # wedge the gate shut forever either.
            self._enforce_calibration_deadline()
            return []

        # Calibrated once motion is small and few contours remain.
        if pct_motion < 0.05 and len(motion_boxes) <= 4:
            self.calibrating = False

        # Lightning/IR/whole-frame change: recalibrate. This does NOT stop
        # detection here; it flips is_calibrating() so the pipeline stops sending
        # regions to the detector while recording continues.
        if self.calibrating or pct_motion > self.config.lightning_threshold:
            self.calibrating = True

        # #373: bound the episode — after calibration_max_frames of
        # consecutive calibrating, force the gate open (with a WARN).
        self._enforce_calibration_deadline()

        alpha = 0.2 if self.calibrating else self.config.frame_alpha
        if motion_boxes:
            self.motion_frame_count += 1
            if self.motion_frame_count >= 10:
                # Only fold a moving frame into the background once it persists,
                # so a briefly-moving object isn't absorbed into the background.
                cv2.accumulateWeighted(resized, self.avg_frame, alpha)
        else:
            cv2.accumulateWeighted(resized, self.avg_frame, alpha)
            self.motion_frame_count = 0

        return motion_boxes
