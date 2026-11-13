# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Baseline-vs-gated benchmark — quantify PR B's expensive-tier savings.

The headline PR B claim is "run the expensive model once per event, not per
frame." This harness measures it: over a stream of Tier-0 tracks it compares

  * **baseline** — the naive always-on cost: one expensive call per confirmed
    track *per frame* (what you'd pay running the costly model every frame);
  * **gated** — the gate's escalations (once per meaningful event);

and reports the **reduction factor** (baseline / gated) plus a **miss rate**
(events — unique confirmed tracks — that never escalated once). The miss rate is
the offline analogue of the runtime ``gate_shadow_*`` instrument.

The core (:func:`run_benchmark`) is pure over a track stream, so it is fully
unit-tested with synthetic tracks — no ffmpeg, no model. :func:`tracks_from_source`
drives a real clip through the pipeline to produce that stream (CLI path). **Do
not publish invented numbers — run this on the real clip set / hardware.**
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from .gate import Gate, GateConfig
from .tracking import Track


@dataclass
class BenchResult:
    frames: int = 0
    baseline_calls: int = 0          # always-on: sum of tracks-per-frame
    gated_calls: int = 0             # gate escalations
    events: int = 0                  # unique confirmed track ids
    missed_events: int = 0           # events that never escalated
    escalations_by_reason: dict[str, int] = field(default_factory=dict)
    model_id: str | None = None      # which model produced this run (for A/B rows)
    wall_seconds: float = 0.0        # wall time of the Tier-0 pass over the clip

    @property
    def fps(self) -> float | None:
        """Sustained Tier-0 throughput — the speed axis. None until timed."""
        return self.frames / self.wall_seconds if self.wall_seconds > 0 else None

    @property
    def reduction_factor(self) -> float:
        # gated==0 with events>0 means everything was suppressed — read miss_rate
        # (==1.0) alongside this; we return baseline as a finite stand-in for "inf".
        return self.baseline_calls / self.gated_calls if self.gated_calls else float(self.baseline_calls)

    @property
    def miss_rate(self) -> float:
        return self.missed_events / self.events if self.events else 0.0

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "fps": round(self.fps, 2) if self.fps is not None else None,
            "frames": self.frames,
            "baseline_calls": self.baseline_calls,
            "gated_calls": self.gated_calls,
            "reduction_factor": round(self.reduction_factor, 2),
            "events": self.events,
            "missed_events": self.missed_events,
            "miss_rate": round(self.miss_rate, 4),
            "escalations_by_reason": self.escalations_by_reason,
        }

    def summary(self) -> str:
        d = self.as_dict()
        tag = f"[{d['model_id']}] " if d.get("model_id") else ""
        fps = f"fps={d['fps']} | " if d.get("fps") is not None else ""
        return (
            f"{tag}{fps}frames={d['frames']} | expensive calls: baseline={d['baseline_calls']} "
            f"gated={d['gated_calls']}  ({d['reduction_factor']}x fewer) | "
            f"events={d['events']} missed={d['missed_events']} (miss-rate {d['miss_rate']})"
        )


def run_benchmark(
    track_frames: Iterable[tuple[list[Track], float]],
    gate_config: GateConfig | None = None,
) -> BenchResult:
    """Compare baseline (always-on) vs gated cost over a stream of ``(tracks, ts)``.

    The gate's *decisions* are identical in shadow or enforce mode (shadow only
    changes whether they're dispatched), so this measures what enforcement would
    do regardless of the config's shadow flag.
    """
    gate = Gate(gate_config or GateConfig(shadow=False))
    res = BenchResult()
    event_ids: set[int] = set()
    escalated_ids: set[int] = set()
    reasons: dict[str, int] = defaultdict(int)

    for tracks, ts in track_frames:
        res.frames += 1
        res.baseline_calls += len(tracks)
        gres = gate.evaluate(tracks, ts)
        for d in gres.decisions:
            if d.escalate:
                res.gated_calls += 1
                escalated_ids.add(d.track_id)
                reasons[d.reason] += 1
        for t in tracks:
            event_ids.add(t.id)

    res.events = len(event_ids)
    res.missed_events = len(event_ids - escalated_ids)
    res.escalations_by_reason = dict(reasons)
    return res


def tracks_from_source(
    frames, *, detector, fps: int = 5, model_size: int = 320
) -> Iterator[tuple[list[Track], float]]:  # pragma: no cover - needs frames/model
    """Drive a real frame source through the Tier-0 pipeline, yielding ``(tracks, ts)``."""
    from .motion import MotionConfig, MotionDetector
    from .pipeline import DetectPipeline
    from .tracking import Tracker, TrackConfig

    pipe = None
    seq = 0
    for frame in frames:
        if pipe is None:
            shape = (frame.height, frame.width)
            pipe = DetectPipeline(
                None, MotionDetector(shape, MotionConfig()), detector,
                Tracker(shape, TrackConfig(fps=fps)),   # thread fps into the lifecycle
                model_size=(model_size, model_size),
            )
        result = pipe.process_frame(frame)
        ts = getattr(frame, "ts", None)
        yield result.tracks, (ts if ts is not None else seq / fps)
        seq += 1


def latency_stats(samples_ms: list[float]) -> dict:
    """Median / p90 / min / mean of per-frame detector times, in ms.

    Median, not mean, is the headline: one slow frame (a GC pause, the OS
    scheduling something else) moves a 30-frame mean but not the median, and
    accelerators have a long first-run tail even after warm-up.
    """
    if not samples_ms:
        return {"n": 0, "median_ms": None, "p90_ms": None, "min_ms": None, "mean_ms": None}
    import math
    import statistics

    s = sorted(samples_ms)
    p90 = s[math.ceil(0.9 * len(s)) - 1]     # nearest rank; round() would tie to even
    return {
        "n": len(s),
        "median_ms": round(statistics.median(s), 2),
        "p90_ms": round(p90, 2),
        "min_ms": round(s[0], 2),
        "mean_ms": round(statistics.fmean(s), 2),
    }


def _frame_to_bgr(frame):
    from .detector import to_bgr
    return to_bgr(frame.data, frame.width, frame.height)


def detector_latency(open_frames, detector, *, n: int = 30, warmup: int = 10,
                     to_image=_frame_to_bgr, clock=None) -> list[float]:
    """Time ``detector.detect`` on whole frames: ``n`` samples after ``warmup``.

    This is the single-stream number in docs/HARDWARE.md ("detector ms/frame"):
    preprocessing, inference and box decoding for one frame, with no motion
    gate, regions or tracker in front — so two devices running the same model
    on the same frames compare directly. Warm-up runs are not timed: the first
    inferences on an NPU or iGPU include graph compilation.

    ``open_frames`` is a zero-argument callable returning a fresh frame
    iterator. A source that ends early (a short clip) is reopened, so ``n`` is
    always met without holding any frame in memory; a live RTSP stream is read
    only as far as needed and closed, so the capture is released at once
    rather than at garbage collection.
    """
    import time

    clock = clock or time.perf_counter
    total = max(0, warmup) + max(1, n)
    samples: list[float] = []
    images = iter(open_frames())
    produced = 0
    try:
        for i in range(total):
            frame = next(images, None)
            if frame is None:
                if not produced:
                    raise RuntimeError("the source produced no frames")
                _close(images)
                images = iter(open_frames())
                produced = 0
                frame = next(images, None)
                if frame is None:
                    raise RuntimeError("the source produced no frames on reopen")
            produced += 1
            img = to_image(frame)
            t0 = clock()
            detector.detect(img)
            if i >= warmup:
                samples.append((clock() - t0) * 1000.0)
    finally:
        _close(images)
    return samples


def _close(it) -> None:
    close = getattr(it, "close", None)
    if close:
        close()


def detector_backend_info(detector) -> dict:
    """What the detector asked ONNX Runtime for and what it really got.

    ``fell_back`` is the line to read before trusting a number: an OpenVINO
    NPU request that ORT could not start runs on CPU without failing, and the
    ms/frame it produces is then a CPU number. ``OrtBackend.requested`` keeps
    the original request through both a provider the wheel lacks and a
    session that failed to build, so the same comparison the service exports
    as ``tier0_detector_backend`` is made here.
    """
    from .onnx_detector import describe_providers, provider_name

    backend = getattr(detector, "_backend", None)
    requested = list(getattr(backend, "requested", None) or [])
    effective = list(getattr(backend, "effective", None) or [])
    wanted = provider_name(requested[0]) if requested else None
    return {
        "backend": getattr(detector, "backend_name", type(detector).__name__),
        "requested": describe_providers(requested) or None,
        "effective": ", ".join(effective) or None,
        "fell_back": bool(
            wanted and wanted != "CPUExecutionProvider"
            and (not effective or effective[0] != wanted)
        ),
        "fallback_error": getattr(backend, "fallback_error", None),
    }


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    import argparse
    import json
    import statistics
    import time

    from .detector import StubDetector
    from .frame_source import VideoFileSource

    p = argparse.ArgumentParser(prog="detect_pipeline.bench", description=__doc__)
    p.add_argument("--source", required=True, help="video file / rtsp url")
    p.add_argument("--detector", choices=["onnx", "blob", "stub"], default="blob")
    p.add_argument("--model", default=None, help="onnx model path")
    p.add_argument("--model-id", default=None,
                   help="label for this model in the output (A/B two builds of the same family)")
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--heartbeat", type=float, default=0.0)
    p.add_argument("--cooldown", type=float, default=30.0)
    p.add_argument("--repeat", type=int, default=1,
                   help="run N times and report fps mean±std — so a small speed delta "
                        "isn't just run-to-run noise")
    p.add_argument("--max-frames", type=int, default=None,
                   help="stop after N frames — required for a live RTSP source, which never ends")
    acc = p.add_argument_group(
        "accelerators (onnx detector)",
        "Same syntax as the service: DETECT_ONNX_PROVIDERS for --providers, and "
        "DETECT_ONNX_PROVIDER_OPTIONS (k=v,k=v) is read from the environment.",
    )
    acc.add_argument("--backend", choices=["auto", "cvdnn", "ort"], default="auto",
                     help="auto: ort when --providers is set, else cvdnn (the service's default)")
    acc.add_argument("--providers", default=None,
                     help="ORT execution providers with an optional :device, e.g. "
                          "OpenVINOExecutionProvider:NPU, OpenVINOExecutionProvider:GPU, "
                          "OpenVINOExecutionProvider:CPU, CPUExecutionProvider")
    acc.add_argument("--input", type=int, default=None,
                     help="model input size (default 640; a fixed-shape model overrides it "
                          "and the row reports the size that ran)")
    acc.add_argument("--threads", type=int, default=None,
                     help="CPU threads for the detector (DETECT_CV_THREADS; the service pins "
                          "ORT to 2, 0 = all cores). The row reports the count that ran")
    acc.add_argument("--latency", type=int, default=0, metavar="N",
                     help="detector-only mode: time N whole frames after --warmup and report "
                          "ms/frame (median, p90) instead of the gated pipeline run")
    acc.add_argument("--warmup", type=int, default=10,
                     help="untimed inferences before --latency samples (NPU/iGPU compile here)")
    args = p.parse_args(argv)

    import os

    accel_flags = {"--backend": args.backend != "auto", "--providers": args.providers is not None,
                   "--input": args.input is not None, "--threads": args.threads is not None}
    if args.detector != "onnx" and any(accel_flags.values()):
        p.error(f"{', '.join(k for k, v in accel_flags.items() if v)} only apply to --detector onnx")
    if args.detector == "onnx" and not args.model:
        p.error("--detector onnx needs --model")
    backend = args.backend
    if backend == "auto":
        backend = "ort" if args.providers else "cvdnn"
    if args.providers and backend != "ort":
        p.error("--providers needs --backend ort (cvdnn has no execution providers)")

    # The thread count that will RUN, not the flag: ORT reads DETECT_CV_THREADS
    # (unset: 2, as in the service; 0: every core), OpenCV its own global.
    threads = args.threads
    if backend == "ort":
        if threads is not None:
            os.environ["DETECT_CV_THREADS"] = str(threads)
        else:
            threads = int((os.environ.get("DETECT_CV_THREADS") or "2").strip() or 0)
        if threads == 0:
            threads = os.cpu_count()
    elif args.detector == "onnx":
        import cv2
        if threads is not None:
            cv2.setNumThreads(threads)
        threads = cv2.getNumThreads()

    def _detector():
        if args.detector == "onnx":
            from .onnx_detector import OnnxYoloDetector
            return OnnxYoloDetector(model_path=args.model, input_size=args.input or 640,
                                    backend=backend, providers=args.providers)
        if args.detector == "blob":
            from .detectors_local import BrightBlobDetector
            return BrightBlobDetector()
        return StubDetector()

    model_id = args.model_id or (
        args.model.rsplit("/", 1)[-1].rsplit(".", 1)[0] if args.model else args.detector
    )

    if args.latency:
        t0 = time.monotonic()
        det = _detector()
        load_s = time.monotonic() - t0
        info = detector_backend_info(det)
        source = VideoFileSource(args.source, max_frames=args.max_frames)
        samples = detector_latency(source.stream, det, n=args.latency, warmup=args.warmup)
        out = {"model_id": model_id, **info,
               "input": getattr(det, "input_size", None), "threads": threads,
               "load_s": round(load_s, 2), "warmup": args.warmup, **latency_stats(samples)}
        on = info["effective"] or info["backend"]
        print(f"[{model_id}] {on}: median={out['median_ms']} ms p90={out['p90_ms']} ms "
              f"min={out['min_ms']} ms over {out['n']} frames | session build {out['load_s']} s")
        if info["fell_back"]:
            print(f"WARNING: asked for {info['requested']} but ran on {on} — "
                  f"these are NOT accelerator numbers. {info['fallback_error'] or ''}".rstrip())
        print(json.dumps(out, indent=2))
        return 2 if info["fell_back"] else 0

    cfg = GateConfig(shadow=False, heartbeat_s=args.heartbeat, escalate_cooldown_s=args.cooldown)

    runs: list[BenchResult] = []
    for _ in range(max(1, args.repeat)):
        frames = VideoFileSource(args.source, max_frames=args.max_frames).stream()
        # Built outside the timer: an NPU/iGPU session compiles its graph here,
        # which is start-up cost, not throughput.
        det = _detector()
        t0 = time.monotonic()
        res = run_benchmark(tracks_from_source(frames, detector=det, fps=args.fps), cfg)
        res.wall_seconds = time.monotonic() - t0
        res.model_id = model_id
        runs.append(res)
        print(res.summary())

    last = runs[-1]
    out = last.as_dict()
    fell_back = False
    if args.detector == "onnx":
        out.update(detector_backend_info(det), input=det.input_size, threads=threads)
        fell_back = out["fell_back"]
        if fell_back:
            print(f"WARNING: asked for {out['requested']} but ran on {out['effective']} — "
                  "these are NOT accelerator numbers.")
    if len(runs) > 1:
        fps_vals = [r.fps for r in runs if r.fps is not None]
        if fps_vals:
            out["fps_mean"] = round(statistics.fmean(fps_vals), 2)
            out["fps_std"] = round(statistics.pstdev(fps_vals), 2) if len(fps_vals) > 1 else 0.0
            print(f"[{model_id}] fps over {len(fps_vals)} runs: "
                  f"mean={out['fps_mean']} std={out['fps_std']}")
    print(json.dumps(out, indent=2))
    return 2 if fell_back else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
