# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DETR-family ONNX detector for Tier-0 — RF-DETR (nano/small/…) first.

RF-DETR is NMS-free: the exported ONNX model emits final detections directly,
so the decode is top-k over sigmoided class logits — no anchors, no NMS, no
IoU threshold to tune. Export contract (rf-detr's ONNX export, also honored by
the community C++/DeepStream integrations):

* input   — ``(1, 3, S, S)`` float32, **RGB**, ImageNet-normalized
            (mean ``[0.485, 0.456, 0.406]``, std ``[0.229, 0.224, 0.225]``),
            where ``S`` is the variant's square resolution (e.g. 384/432/560).
* ``dets``   — ``(1, Q, 4)`` boxes, **cxcywh normalized to [0, 1]** of the input.
* ``labels`` — ``(1, Q, C)`` raw class logits (apply sigmoid).

Class indexing follows the DETR/COCO convention, which differs from YOLO's
contiguous 80: ``C == 91`` means index = COCO category id (0 = background,
with gaps); ``C == 90`` is the same table shifted by one (background dropped);
``C == 80`` is the contiguous YOLO-style list. ``labels_for_c`` picks the
right table from ``C`` so a mis-mapped "person" can't silently become "bicycle".

Reuses the backend seam from :mod:`onnx_detector` (``cvdnn`` | ``ort``) via the
multi-output ``infer_all``. Transformer exports commonly exceed cv2.dnn's
operator coverage — ``ort`` is the expected backend; ``cvdnn`` is attempted
only if asked, and a load failure names the fix.

Getting weights (documented, never vendored — weights stay runtime-downloaded):

    pip install rfdetr
    python -c "from rfdetr import RFDETRNano; RFDETRNano().export()"  # → onnx

The decode is a pure function tested on synthetic tensors; the backend is
injectable, so nothing here needs a model file or onnxruntime at test time.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from .detector import RawDetection
from .onnx_detector import COCO_LABELS, build_backend

log = logging.getLogger("detect_pipeline.detr_detector")

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# COCO 91-slot table: index = COCO category id; None = background / unused id.
COCO91_LABELS: list[str | None] = [None] * 91
for _i, _lbl in zip(
    # the 80 populated category ids, in order
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21,
     22, 23, 24, 25, 27, 28, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42,
     43, 44, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61,
     62, 63, 64, 65, 67, 70, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 84,
     85, 86, 87, 88, 89, 90],
    COCO_LABELS,
):
    COCO91_LABELS[_i] = _lbl
del _i, _lbl


def labels_for_c(num_classes: int) -> list[str | None]:
    """Pick the label table matching the model's class-logit width."""
    if num_classes == 91:
        return COCO91_LABELS
    if num_classes == 90:
        return COCO91_LABELS[1:]          # background slot dropped by the export
    if num_classes == 80:
        return list(COCO_LABELS)
    log.warning("DETR head has %d classes — not a known COCO layout; "
                "labels fall back to raw indices", num_classes)
    return [str(i) for i in range(num_classes)]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    # Extreme logits overflow float32 exp() into a RuntimeWarning; the result
    # (0.0 / 1.0) is still correct, so silence the noise rather than warn per
    # frame on a confident model.
    with np.errstate(over="ignore"):
        return 1.0 / (1.0 + np.exp(-x))


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def postprocess_detr(
    dets: np.ndarray,
    logits: np.ndarray,
    *,
    conf_threshold: float = 0.4,
    top_k: int = 100,
    labels: list[str | None] | None = None,
) -> list[RawDetection]:
    """Decode RF-DETR outputs into normalized detections. Pure; NMS-free.

    ``dets``: ``(1, Q, 4)`` or ``(Q, 4)`` cxcywh in [0, 1] of the model input.
    ``logits``: ``(1, Q, C)`` or ``(Q, C)`` raw class logits.
    Selection is DETR-standard: sigmoid, then top-k over ALL (query, class)
    pairs, then the confidence floor. Background/gap slots are dropped.
    """
    boxes = np.asarray(dets, dtype=np.float32)
    scores = np.asarray(logits, dtype=np.float32)
    # Strip ONLY the batch dim — a blind squeeze() would also collapse a
    # single-query axis and silently return nothing.
    if boxes.ndim == 3:
        boxes = boxes[0]
    if scores.ndim == 3:
        scores = scores[0]
    if boxes.ndim != 2 or scores.ndim != 2 or boxes.shape[0] != scores.shape[0]:
        return []
    if boxes.shape[1] != 4 or scores.shape[1] < 1:
        # Malformed export (wrong box width) must yield nothing, not a
        # ValueError that kills the worker's frame loop.
        return []
    q, c = scores.shape
    table = labels if labels is not None else labels_for_c(c)
    probs = _sigmoid(scores)

    flat = probs.reshape(-1)
    k = min(int(top_k), flat.size)
    if k <= 0:
        return []
    top = np.argpartition(-flat, k - 1)[:k]
    top = top[np.argsort(-flat[top])]

    out: list[RawDetection] = []
    for idx in top:
        score = float(flat[idx])
        if score < conf_threshold:
            break                          # sorted — everything after is lower
        qi, ci = divmod(int(idx), c)
        label = table[ci] if 0 <= ci < len(table) else None
        if label is None:                  # background / unused COCO gap
            continue
        cx, cy, w, h = boxes[qi]
        out.append(
            RawDetection(
                label,
                score,
                (
                    _clamp01(float(cx - w / 2.0)),
                    _clamp01(float(cy - h / 2.0)),
                    _clamp01(float(cx + w / 2.0)),
                    _clamp01(float(cy + h / 2.0)),
                ),
            )
        )
    return out


def preprocess_detr(crop: np.ndarray, input_size: int) -> np.ndarray:
    """BGR crop → ``(1, 3, S, S)`` float32 RGB blob, ImageNet-normalized."""
    resized = cv2.resize(crop, (input_size, input_size), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - _IMAGENET_MEAN) / _IMAGENET_STD
    return np.transpose(rgb, (2, 0, 1))[np.newaxis, ...]


class OnnxDetrDetector:
    """RF-DETR-family ONNX detector over the shared backend seam.

    Same ``DetectorAdapter`` contract as ``OnnxYoloDetector`` — hand it a BGR
    crop, get back normalized ``RawDetection``s — so the pipeline, tracker,
    gate, and eval harness treat the two families identically.
    """

    def __init__(
        self,
        model_path: str | None = None,
        *,
        input_size: int = 384,
        conf_threshold: float = 0.4,
        top_k: int = 100,
        backend: str = "ort",
        providers=None,
        backend_impl=None,                 # inject a fake backend for tests
    ) -> None:
        self.input_size = input_size
        self.conf_threshold = conf_threshold
        self.top_k = top_k
        if backend_impl is not None:
            self._backend = backend_impl
        elif model_path:
            self._backend = build_backend(backend, model_path, providers=providers)
        else:
            raise ValueError("OnnxDetrDetector needs a model_path or backend_impl")
        self.backend_name = getattr(self._backend, "name", "custom")
        # RF-DETR variants ship at 384/432/560; a mismatch used to fail on
        # every region forever. Settle it against the real graph once.
        if model_path:
            from .detector import resolve_input_size
            self.input_size = resolve_input_size(self, self.input_size)

    def detect(self, crop: np.ndarray) -> list[RawDetection]:
        blob = preprocess_detr(crop, self.input_size)
        outputs = self._backend.infer_all(blob)
        if not outputs or len(outputs) < 2:
            return []
        # Convention: dets is the (…, 4) tensor, labels the (…, C) tensor —
        # identified by shape so output ORDER never silently breaks decode.
        dets = next((o for o in outputs if np.asarray(o).shape[-1] == 4), None)
        logits = next((o for o in outputs if np.asarray(o).shape[-1] != 4), None)
        if dets is None or logits is None:
            return []
        return postprocess_detr(
            dets, logits,
            conf_threshold=self.conf_threshold, top_k=self.top_k,
        )
