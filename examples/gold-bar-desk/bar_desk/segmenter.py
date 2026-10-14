# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastSAM-s through onnxruntime: gold bars in a tray crop.

FastSAM-s is a YOLOv8-seg graph. output0 is [1, 37, N]: box (cx, cy,
w, h), one class score, 32 mask coefficients. output1 is [1, 32, 160,
160] mask prototypes. A mask is sigmoid(coefficients @ prototypes),
cut to its box.

Bars are tracked by the detection boxes, which are full precision; the
masks (160x160 for the whole 640 input) are only used to check that
what was boxed is mostly gold-coloured.

Checked against the ultralytics pipeline this replaces, on a minute of
desk footage at 1080p: every sampled frame within one bar of it, most
identical, at about half a second a frame on two CPU threads.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np

INPUT = 640


@dataclass
class BarFilter:
    # Box area as a fraction of the whole FRAME, so the same numbers hold
    # at any resolution. (A 1 kg bar seen from about 2 m at 1080p is
    # 1100-6000 px.)
    min_area: float = 0.00053
    max_area: float = 0.0029
    min_aspect: float = 1.4      # height / width: bars stand upright
    max_aspect: float = 3.2
    min_gold: float = 0.6        # share of the mask that is gold-coloured
    min_value: float = 110.0     # median brightness inside the mask


def letterbox(img, size=INPUT):
    h, w = img.shape[:2]
    r = size / max(h, w)
    nw, nh = int(round(w * r)), int(round(h * r))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top, left = (size - nh) // 2, (size - nw) // 2
    canvas = np.full((size, size, 3), 114, np.uint8)
    canvas[top:top + nh, left:left + nw] = resized
    return canvas, r, left, top


def decode(out0, out1, *, conf=0.25, iou=0.7, max_det=300):
    """Raw graph outputs -> (boxes xyxy in 640 space, scores, masks 160x160)."""
    pred = out0[0].T                      # [N, 37]
    scores = pred[:, 4]
    keep = scores > conf
    pred, scores = pred[keep], scores[keep]
    if not len(pred):
        return np.zeros((0, 4)), np.zeros(0), np.zeros((0, 160, 160), np.float32)
    cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 1)
    idx = cv2.dnn.NMSBoxes([[float(a), float(b), float(c), float(d)] for a, b, c, d in
                            np.stack([xyxy[:, 0], xyxy[:, 1], w, h], 1)],
                           scores.astype(float).tolist(), conf, iou)
    idx = np.array(idx, dtype=int).reshape(-1)[:max_det]
    xyxy, scores, coef = xyxy[idx], scores[idx], pred[idx, 5:]
    protos = out1[0].reshape(32, -1)
    masks = 1.0 / (1.0 + np.exp(-np.clip(coef @ protos, -50, 50)))
    return xyxy, scores, masks.reshape(-1, 160, 160).astype(np.float32)


class Segmenter:
    def __init__(self, model_path, threads=2):
        # onnxruntime (1.31's Linux build carries Microsoft's 1DS client)
        # reports usage to mobile.events.data.microsoft.com from the moment
        # it is IMPORTED. This app declares no egress, so the platform's
        # proxy refused every attempt and raised an alert for each one —
        # and a camera app has no business phoning home anyway. Only the
        # environment variable, set before import, stops it; the Dockerfile
        # sets it too, and the API call below is belt and braces.
        os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
        import onnxruntime as ort
        disable = getattr(ort, "disable_telemetry_events", None)
        if disable is not None:
            disable()
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(model_path), so,
                                         providers=["CPUExecutionProvider"])
        self.input = self.sess.get_inputs()[0].name

    def raw(self, crop):
        img, r, left, top = letterbox(crop)
        x = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out0, out1 = self.sess.run(None, {self.input: x})
        return decode(out0, out1), r, left, top

    def bars(self, crop, frame_area: float, flt: BarFilter | None = None):
        """Gold bars in ``crop`` as (x, y, w, h) boxes in crop pixels.
        ``frame_area`` is the full frame's pixel count, which the size
        limits are fractions of."""
        flt = flt or BarFilter()
        lo, hi = flt.min_area * frame_area, flt.max_area * frame_area
        (xyxy, _scores, masks), r, left, top = self.raw(crop)
        H, W = crop.shape[:2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        goldish = (((hsv[..., 0] >= 10) & (hsv[..., 0] <= 38) &
                    (hsv[..., 1] >= 45) & (hsv[..., 2] >= 95)) | (hsv[..., 2] >= 200))
        found = []
        for (x0, y0, x1, y1), m in zip(xyxy, masks):
            # 640-letterbox -> crop pixels
            bx0 = max(0, int((x0 - left) / r)); by0 = max(0, int((y0 - top) / r))
            bx1 = min(W, int((x1 - left) / r)); by1 = min(H, int((y1 - top) / r))
            bw, bh = bx1 - bx0, by1 - by0
            if bw <= 2 or bh <= 2:
                continue
            area = bw * bh
            if not lo <= area <= hi:
                continue
            if not flt.min_aspect <= bh / bw <= flt.max_aspect:
                continue
            # the mask, at crop resolution, only inside this box
            mx0, my0 = (bx0 * r + left) / 4, (by0 * r + top) / 4
            mx1, my1 = (bx1 * r + left) / 4, (by1 * r + top) / 4
            sub = m[int(my0):max(int(my0) + 1, int(np.ceil(my1))),
                    int(mx0):max(int(mx0) + 1, int(np.ceil(mx1)))]
            sub = cv2.resize(sub, (bw, bh), interpolation=cv2.INTER_LINEAR) > 0.5
            if sub.sum() < 0.3 * area:
                continue
            g = goldish[by0:by1, bx0:bx1][sub]
            v = hsv[by0:by1, bx0:bx1, 2][sub]
            if g.mean() < flt.min_gold or np.median(v) < flt.min_value:
                continue
            found.append((area, (bx0, by0, bw, bh)))
        # the same bar boxed twice: keep the bigger box
        out = []
        for area, b in sorted(found, key=lambda f: -f[0]):
            if all(_iou(b, o) < 0.3 for o in out):
                out.append(b)
        return out


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter or 1)
