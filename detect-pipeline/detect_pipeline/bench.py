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

    @property
    def reduction_factor(self) -> float:
        return self.baseline_calls / self.gated_calls if self.gated_calls else float(self.baseline_calls)

    @property
    def miss_rate(self) -> float:
        return self.missed_events / self.events if self.events else 0.0

    def as_dict(self) -> dict:
        return {
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
        return (
            f"frames={d['frames']} | expensive calls: baseline={d['baseline_calls']} "
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
    from .tracking import Tracker

    pipe = None
    seq = 0
    for frame in frames:
        if pipe is None:
            shape = (frame.height, frame.width)
            pipe = DetectPipeline(
                None, MotionDetector(shape, MotionConfig()), detector,
                Tracker(shape), model_size=(model_size, model_size),
            )
        result = pipe.process_frame(frame)
        ts = getattr(frame, "ts", None)
        yield result.tracks, (ts if ts is not None else seq / fps)
        seq += 1


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    import argparse
    import json

    from .detector import StubDetector
    from .frame_source import VideoFileSource

    p = argparse.ArgumentParser(prog="detect_pipeline.bench", description=__doc__)
    p.add_argument("--source", required=True, help="video file / rtsp url")
    p.add_argument("--detector", choices=["onnx", "blob", "stub"], default="blob")
    p.add_argument("--model", default=None)
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--heartbeat", type=float, default=0.0)
    p.add_argument("--cooldown", type=float, default=30.0)
    args = p.parse_args(argv)

    if args.detector == "onnx":
        from .onnx_detector import OnnxYoloDetector
        det = OnnxYoloDetector(model_path=args.model)
    elif args.detector == "blob":
        from .detectors_local import BrightBlobDetector
        det = BrightBlobDetector()
    else:
        det = StubDetector()

    frames = VideoFileSource(args.source).stream()
    cfg = GateConfig(shadow=False, heartbeat_s=args.heartbeat, escalate_cooldown_s=args.cooldown)
    res = run_benchmark(tracks_from_source(frames, detector=det, fps=args.fps), cfg)
    print(res.summary())
    print(json.dumps(res.as_dict(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
