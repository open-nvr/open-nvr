# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The pipeline that turns a camera frame into a recognised-face record.

Single-stage: POST the frame to the InsightFace adapter via KAI-C,
parse the §11.5-style response, return a typed ``FaceRead``.

Kept separate from ``smart_doorbell.py`` so it can be tested without
the daemon loop, the config loader, or the alert dispatcher. A
test passes in a fake ``RecognitionClient`` that returns canned
responses; the pipeline asserts only on the parser + the
known/unknown decision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


DEFAULT_RECOGNITION_THRESHOLD: float = 0.5


@dataclass(frozen=True)
class FaceRead:
    """The pipeline's output. One per processed frame; ``None``
    bubbles up from ``process_frame`` when no face was detected at
    all (caller can decide whether to fire a 'movement, no face'
    alert or just drop it)."""

    # True iff a face was detected (regardless of recognition).
    face_detected: bool
    # True iff the detected face matched a registered person above
    # the threshold. Implies ``face_detected``.
    recognized: bool
    # Filled in when ``recognized`` — the person_id / name /
    # category from the face DB.
    person_id: str | None = None
    name: str | None = None
    category: str | None = None
    # Cosine similarity to the matched face when ``recognized``; to the
    # NEAREST enrolled face when not (below the threshold by
    # definition, and unnamed). ``None`` when nobody is enrolled or the
    # adapter predates the field. The doorbell reads a near miss as
    # "wait for a better frame", not "stranger".
    similarity: float | None = None
    # Pixel bbox of the face on the source frame, if reported.
    face_bbox: tuple[int, int, int, int] | None = None
    # The detector's confidence in the face itself (not the match).
    # Low on a blurred half-face at the edge of the frame — the frames
    # a doorbell should not decide from. ``None`` from older adapters.
    confidence: float | None = None
    # Threshold that was applied. Useful in audit / debug.
    threshold: float | None = None
    # Adapter-side correlation ID; threaded through to the alert
    # so the audit log joins frame → KAI-C → adapter → alert.
    correlation_id: str | None = None
    # Raw adapter result block for callers that want extra fields
    # (gender / age / message / etc.).
    raw: dict[str, Any] = field(default_factory=dict)


class RecognitionClient(Protocol):
    """Anything that turns ``frame_bytes`` into a §5-style adapter
    response is a recognition client. The default implementation in
    smart_doorbell.py POSTs through KAI-C; tests pass in a stub."""

    def recognize(
        self,
        frame_jpeg: bytes,
        *,
        threshold: float,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        ...  # pragma: no cover — Protocol


@dataclass
class FaceRecognitionPipelineConfig:
    """Per-camera tuning knobs the orchestrator passes in."""
    recognition_threshold: float = DEFAULT_RECOGNITION_THRESHOLD

    def __post_init__(self) -> None:
        if not 0.0 < self.recognition_threshold <= 1.0:
            raise ValueError(
                "recognition_threshold must be in (0.0, 1.0]; "
                f"got {self.recognition_threshold}"
            )


class FaceRecognitionPipeline:
    """Drives one frame through the InsightFace adapter."""

    def __init__(
        self,
        client: RecognitionClient,
        config: FaceRecognitionPipelineConfig | None = None,
    ) -> None:
        self.client = client
        self.config = config or FaceRecognitionPipelineConfig()

    def process_frame(
        self,
        frame_jpeg: bytes,
        *,
        correlation_id: str | None = None,
    ) -> FaceRead | None:
        """Run one inference call. Returns:

        * ``None`` if the adapter call failed (logged, caller drops
          the frame).
        * ``FaceRead(face_detected=False, ...)`` if no face was
          detected — caller decides whether that's interesting.
        * ``FaceRead(face_detected=True, recognized=False, ...)`` for
          an unknown face (the canonical "alert me, stranger at the
          door" path).
        * ``FaceRead(face_detected=True, recognized=True, ...)`` with
          person details for a recognised face.
        """
        try:
            response = self.client.recognize(
                frame_jpeg,
                threshold=self.config.recognition_threshold,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception("face recognition call failed; dropping frame")
            return None

        return _parse_recognition_response(response, correlation_id=correlation_id)


def _parse_recognition_response(
    response: dict[str, Any],
    *,
    correlation_id: str | None,
) -> FaceRead | None:
    """Turn an adapter ``InferResponse`` body into a ``FaceRead``."""
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None

    bbox = _parse_bbox(result.get("face_bbox"))
    threshold = _coerce_float(result.get("threshold"))
    confidence = _coerce_float(result.get("confidence"))

    if not result.get("recognized"):
        # Adapter reports either "no face detected" or "face below
        # threshold." Both surface as face_detected based on whether
        # there's a bbox — bbox present means "face seen, just not
        # matched." A miss may carry the nearest similarity and the
        # detector's confidence; both are optional on the wire.
        return FaceRead(
            face_detected=bbox is not None,
            recognized=False,
            similarity=_coerce_float(result.get("similarity")) if bbox else None,
            face_bbox=bbox,
            confidence=confidence if bbox else None,
            threshold=threshold,
            correlation_id=correlation_id,
            raw=dict(result),
        )

    # Recognised — pull person details. ``similarity`` is required
    # by the adapter contract when recognized is true but we
    # tolerate a missing value (treat as None) rather than dropping
    # the whole read.
    return FaceRead(
        face_detected=True,
        recognized=True,
        person_id=_coerce_str(result.get("person_id")),
        name=_coerce_str(result.get("name")),
        category=_coerce_str(result.get("category")),
        similarity=_coerce_float(result.get("similarity")),
        face_bbox=bbox,
        confidence=confidence,
        threshold=threshold,
        correlation_id=correlation_id,
        raw=dict(result),
    )


# ── coercion helpers ───────────────────────────────────────────────


def _parse_bbox(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
