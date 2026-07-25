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
    args = p.parse_args(argv)

    def _detector():
        if args.detector == "onnx":
            from .onnx_detector import OnnxYoloDetector
            return OnnxYoloDetector(model_path=args.model)
        if args.detector == "blob":
            from .detectors_local import BrightBlobDetector
            return BrightBlobDetector()
        return StubDetector()

    model_id = args.model_id or (
        args.model.rsplit("/", 1)[-1].rsplit(".", 1)[0] if args.model else args.detector
    )
    cfg = GateConfig(shadow=False, heartbeat_s=args.heartbeat, escalate_cooldown_s=args.cooldown)

    runs: list[BenchResult] = []
    for _ in range(max(1, args.repeat)):
        frames = VideoFileSource(args.source).stream()   # fresh generator each run
        t0 = time.monotonic()
        res = run_benchmark(tracks_from_source(frames, detector=_detector(), fps=args.fps), cfg)
        res.wall_seconds = time.monotonic() - t0
        res.model_id = model_id
        runs.append(res)
        print(res.summary())

    last = runs[-1]
    out = last.as_dict()
    if len(runs) > 1:
        fps_vals = [r.fps for r in runs if r.fps is not None]
        if fps_vals:
            out["fps_mean"] = round(statistics.fmean(fps_vals), 2)
            out["fps_std"] = round(statistics.pstdev(fps_vals), 2) if len(fps_vals) > 1 else 0.0
            print(f"[{model_id}] fps over {len(fps_vals)} runs: "
                  f"mean={out['fps_mean']} std={out['fps_std']}")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
