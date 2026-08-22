# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Detector eval harness — make a detector swap a measured decision, not vibes.

Replays a clip (or recorded site footage) through two or more detectors and
reports, side by side: per-frame latency (mean / p95), per-label detection
volume, and pairwise agreement against a reference detector — matched
detections (same label, IoU ≥ 0.5), plus what only the reference saw
("missed") and what only the candidate saw ("extra"). With a *stronger*
reference model (e.g. yolov8m) the missed column reads as a recall proxy;
between two candidates it reads as behavioral drift to investigate.

    python -m detect_pipeline.evalcmp --source clip.mp4 \
        --model yolov8n=weights/yolov8n.onnx:yolo:cvdnn:640 \
        --model rfdetr=weights/rfdetr-nano.onnx:detr:ort:384 \
        [--reference yolov8m=weights/yolov8m.onnx:yolo:cvdnn:640] \
        [--conf 0.4] [--every 12] [--max-frames 500] [--json out.json]

Spec grammar: ``name=path:family:backend[:input]`` — family ``yolo`` | ``detr``,
backend ``cvdnn`` | ``ort``. Frames are read with OpenCV, sampled every
``--every``-th frame (≈ DETECT_FPS against a 25 fps clip at the default 12),
and each detector sees the SAME full frame resized to its own input square —
deliberately simpler than the production motion→region path so the comparison
isolates the models. Matching, aggregation, and spec parsing are pure
functions; the CLI wires them to real models.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field

from .detector import RawDetection

IOU_MATCH = 0.5


# ── pure pieces ────────────────────────────────────────────────────────


@dataclass
class ModelSpec:
    name: str
    path: str
    family: str        # yolo | detr
    backend: str       # cvdnn | ort
    input_size: int


def parse_model_spec(text: str, *, default_input: int = 640) -> ModelSpec:
    """``name=path:family:backend[:input]`` → ModelSpec (raises ValueError)."""
    name, eq, rest = text.partition("=")
    if not eq or not name or not rest:
        raise ValueError(f"model spec needs name=path:family:backend, got {text!r}")
    parts = rest.split(":")
    if len(parts) < 3:
        raise ValueError(f"model spec needs path:family:backend, got {rest!r}")
    path, family, backend = parts[0], parts[1].lower(), parts[2].lower()
    if family not in ("yolo", "detr"):
        raise ValueError(f"unknown family {family!r} (yolo | detr)")
    if backend not in ("cvdnn", "ort"):
        raise ValueError(f"unknown backend {backend!r} (cvdnn | ort)")
    input_size = int(parts[3]) if len(parts) > 3 and parts[3] else default_input
    return ModelSpec(name, path, family, backend, input_size)


def iou(a: tuple, b: tuple) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def match_frame(
    reference: list[RawDetection], candidate: list[RawDetection],
    *, iou_threshold: float = IOU_MATCH,
) -> tuple[int, int, int]:
    """Greedy same-label IoU matching → (matched, missed, extra).

    ``missed`` = reference detections with no candidate match; ``extra`` =
    candidate detections with no reference match. Greedy best-IoU-first is the
    standard cheap approximation of optimal assignment.
    """
    pairs = [
        (iou(r.box, c.box), ri, ci)
        for ri, r in enumerate(reference)
        for ci, c in enumerate(candidate)
        if r.label == c.label
    ]
    pairs.sort(reverse=True)
    used_r: set[int] = set()
    used_c: set[int] = set()
    matched = 0
    for score, ri, ci in pairs:
        if score < iou_threshold:
            break
        if ri in used_r or ci in used_c:
            continue
        used_r.add(ri)
        used_c.add(ci)
        matched += 1
    return matched, len(reference) - matched, len(candidate) - matched


@dataclass
class ModelStats:
    frames: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    label_counts: dict[str, int] = field(default_factory=dict)
    matched: int = 0
    missed: int = 0
    extra: int = 0

    def note(self, dets: list[RawDetection], latency_ms: float) -> None:
        self.frames += 1
        self.latencies_ms.append(latency_ms)
        for d in dets:
            self.label_counts[d.label] = self.label_counts.get(d.label, 0) + 1

    def summary(self) -> dict:
        lats = sorted(self.latencies_ms)
        n = len(lats)
        agree_base = self.matched + self.missed
        return {
            "frames": self.frames,
            "ms_mean": round(sum(lats) / n, 2) if n else None,
            "ms_p95": round(lats[min(n - 1, int(n * 0.95))], 2) if n else None,
            "detections": sum(self.label_counts.values()),
            "labels": dict(sorted(self.label_counts.items(),
                                  key=lambda kv: -kv[1])),
            "matched": self.matched,
            "missed": self.missed,
            "extra": self.extra,
            "agreement": round(self.matched / agree_base, 3) if agree_base else None,
        }


# ── wiring (real models; kept thin) ────────────────────────────────────


def build_detector(spec: ModelSpec, conf: float):
    if spec.family == "yolo":
        from .onnx_detector import OnnxYoloDetector
        return OnnxYoloDetector(
            model_path=spec.path, input_size=spec.input_size,
            backend=spec.backend, conf_threshold=conf,
        )
    from .detr_detector import OnnxDetrDetector
    return OnnxDetrDetector(
        model_path=spec.path, input_size=spec.input_size,
        backend=spec.backend, conf_threshold=conf,
    )


def run_eval(
    source: str, specs: list[ModelSpec], *, reference: ModelSpec | None = None,
    conf: float = 0.4, every: int = 12, max_frames: int | None = 500,
    detectors: dict | None = None,   # injectable {name: DetectorAdapter} for tests
) -> dict:
    from .detector import to_bgr
    from .frame_source import VideoFileSource

    all_specs = ([reference] if reference else []) + specs
    dets = detectors or {s.name: build_detector(s, conf) for s in all_specs}
    stats = {s.name: ModelStats() for s in all_specs}
    ref_name = reference.name if reference else specs[0].name

    n = 0
    for frame in VideoFileSource(source).stream():
        if frame.seq % max(1, every):
            continue
        n += 1
        if max_frames is not None and n > max_frames:
            break
        bgr = to_bgr(frame.data, frame.width, frame.height)
        per_model: dict[str, list[RawDetection]] = {}
        for s in all_specs:
            t0 = time.perf_counter()
            found = dets[s.name].detect(bgr)
            stats[s.name].note(found, (time.perf_counter() - t0) * 1000.0)
            per_model[s.name] = found
        ref_dets = per_model[ref_name]
        for s in all_specs:
            if s.name == ref_name:
                continue
            m, miss, extra = match_frame(ref_dets, per_model[s.name])
            st = stats[s.name]
            st.matched += m
            st.missed += miss
            st.extra += extra
    return {
        "source": source,
        "reference": ref_name,
        "frames_evaluated": min(n, max_frames) if max_frames else n,
        "models": {name: st.summary() for name, st in stats.items()},
    }


def _print_table(result: dict) -> None:
    ref = result["reference"]
    print(f"\nsource: {result['source']}   frames: {result['frames_evaluated']}"
          f"   reference: {ref}\n")
    hdr = f"{'model':<14}{'ms/frame':>10}{'p95':>8}{'dets':>7}{'matched':>9}{'missed':>8}{'extra':>7}{'agree':>7}"
    print(hdr)
    print("-" * len(hdr))
    for name, s in result["models"].items():
        agree = "-" if name == ref else (s["agreement"] if s["agreement"] is not None else "-")
        matched = "-" if name == ref else s["matched"]
        missed = "-" if name == ref else s["missed"]
        extra = "-" if name == ref else s["extra"]
        print(f"{name:<14}{s['ms_mean'] or 0:>10}{s['ms_p95'] or 0:>8}"
              f"{s['detections']:>7}{matched:>9}{missed:>8}{extra:>7}{agree:>7}")
    print("\nper-label volume:")
    for name, s in result["models"].items():
        top = ", ".join(f"{k}={v}" for k, v in list(s["labels"].items())[:8])
        print(f"  {name:<12} {top or '(none)'}")
    print("\nreading it: 'missed' vs a STRONGER reference ≈ recall gap; between"
          "\npeers it is drift to eyeball. Latency is detector-only (no decode).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare detectors on a clip.")
    ap.add_argument("--source", required=True, help="video file / clip to replay")
    ap.add_argument("--model", action="append", required=True,
                    help="name=path:family:backend[:input] (repeatable)")
    ap.add_argument("--reference", default=None,
                    help="optional reference spec (else the first --model)")
    ap.add_argument("--conf", type=float, default=0.4)
    ap.add_argument("--every", type=int, default=12,
                    help="sample every Nth frame (≈DETECT_FPS on a 25fps clip)")
    ap.add_argument("--max-frames", type=int, default=500)
    ap.add_argument("--json", default=None, help="also write the result as JSON")
    args = ap.parse_args(argv)

    specs = [parse_model_spec(m) for m in args.model]
    ref = parse_model_spec(args.reference) if args.reference else None
    result = run_eval(
        args.source, specs, reference=ref, conf=args.conf,
        every=args.every, max_frames=args.max_frames,
    )
    _print_table(result)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
