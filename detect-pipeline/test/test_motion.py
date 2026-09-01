# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ported motion detector."""
from __future__ import annotations

import numpy as np

from detect_pipeline.motion import MotionConfig, MotionDetector

H, W = 200, 200


def _flat(value: int) -> np.ndarray:
    return np.full((H, W), value, np.uint8)


def _warm_up(md: MotionDetector, value: int = 120, frames: int = 60) -> None:
    """Feed a static scene until the detector calibrates."""
    for _ in range(frames):
        md.detect(_flat(value))


def test_disabled_gate_is_wide_open():
    """#373 semantics change: enabled=False used to return [] while
    ``calibrating`` stayed True — which WEDGED the pipeline (never
    calibrated, never detected). Disabling the gate now means the gate
    is OPEN: never calibrating, full-frame motion every frame, so the
    detector always runs."""
    md = MotionDetector((H, W), MotionConfig(enabled=False))
    assert md.is_calibrating() is False
    assert md.detect(_flat(255)) == [(0, 0, W, H)]
    assert md.is_calibrating() is False


def test_static_scene_calibrates_and_goes_quiet():
    md = MotionDetector((H, W))
    _warm_up(md)
    assert md.is_calibrating() is False
    assert md.detect(_flat(120)) == []      # no motion on a settled static scene


def test_moving_object_produces_a_box_in_region():
    md = MotionDetector((H, W))
    _warm_up(md)
    frame = _flat(120)
    frame[40:80, 140:180] = 255             # bright patch, bottom-right-ish
    boxes = md.detect(frame)
    assert boxes, "a bright patch on a calibrated scene should produce motion"
    x1, y1, x2, y2 = boxes[0]
    # coords are in full-frame space and within bounds
    assert 0 <= x1 < x2 <= W and 0 <= y1 < y2 <= H
    # roughly localized to the patch region (right/lower half), not the whole frame
    assert x2 > W // 2 and y2 > H // 4
    assert (x2 - x1) < W and (y2 - y1) < H


def test_whole_frame_flash_sets_calibrating():
    md = MotionDetector((H, W))
    _warm_up(md)
    assert md.is_calibrating() is False
    md.detect(_flat(255))                   # dawn/IR-cut style full-frame change
    assert md.is_calibrating() is True      # pipeline will stop sending regions


def test_skip_motion_threshold_drops_frame():
    md = MotionDetector((H, W), MotionConfig(skip_motion_threshold=0.5))
    _warm_up(md)
    boxes = md.detect(_flat(255))           # ~100% change > 0.5
    assert boxes == []
    assert md.is_calibrating() is True


def test_boxes_scale_to_full_frame_when_downscaled():
    # frame_height 100 on a 200-tall input → resize_factor 2 → boxes scaled up.
    md = MotionDetector((H, W), MotionConfig(frame_height=100))
    assert md.resize_factor == 2.0
    _warm_up(md)
    frame = _flat(120)
    frame[100:160, 100:160] = 255
    boxes = md.detect(frame)
    assert boxes
    x1, y1, x2, y2 = boxes[0]
    assert x2 <= W and y2 <= H              # scaled coords stay within the full frame


# ── Issue #373: the calibration wedge ──────────────────────────────
#
# Calibration clears only on a frame with <5% motion and <=4 boxes. A
# scene with CONTINUOUS moderate motion (trees, rain, a busy road)
# never produces one — QA saw 1629/1629 frames skipped as
# "calibrating" over 13 minutes: detector never ran, no visits, no
# plates, no signal anywhere. The deadline bounds every calibration
# episode; the env plumbing (tested below) makes the gate tunable.

def _busy_frame(rng: np.random.Generator) -> np.ndarray:
    """A frame where ~a third of the scene churns every call — motion
    every frame (pct ~0.3: above the 0.05 calibration bar, below the
    0.8 lightning bar), which is exactly the #373 wedge regime."""
    frame = _flat(120)
    frame[0:120, 0:100] = rng.integers(0, 255, (120, 100), dtype=np.uint8)
    return frame


def test_continuous_motion_wedges_forever_without_deadline():
    """Pin the BUG first (deadline off = pre-#373 behaviour): the gate
    never opens, ever — this is what QA lived through, and what the
    deadline exists to bound."""
    md = MotionDetector((H, W), MotionConfig(calibration_max_frames=0))
    rng = np.random.default_rng(42)
    for _ in range(300):
        md.detect(_busy_frame(rng))
    assert md.is_calibrating() is True
    assert md.forced_calibration_exits == 0


def test_deadline_forces_the_gate_open_on_a_busy_scene():
    """The fix: same scene, deadline on — after calibration_max_frames
    consecutive calibrating frames the gate forces itself open and the
    detector gets to run."""
    md = MotionDetector(
        (H, W), MotionConfig(calibration_max_frames=25), label="cam5",
    )
    rng = np.random.default_rng(42)
    opened_at = None
    for i in range(60):
        md.detect(_busy_frame(rng))
        if opened_at is None and not md.is_calibrating():
            opened_at = i + 1
    assert opened_at == 25, f"gate should open exactly at the deadline, got {opened_at}"
    assert md.forced_calibration_exits >= 1
    # And it STAYS open on this scene (motion stays under the
    # lightning threshold, so nothing re-trips calibration).
    assert md.is_calibrating() is False


def test_natural_calibration_never_trips_the_deadline():
    """A normal static scene calibrates in a handful of frames — the
    deadline must never fire there, and the episode counter resets."""
    md = MotionDetector((H, W), MotionConfig(calibration_max_frames=150))
    _warm_up(md)
    assert md.is_calibrating() is False
    assert md.forced_calibration_exits == 0
    assert md.calibrating_frames == 0


def test_lightning_recalibration_episode_is_also_bounded():
    """A whole-frame change flips calibrating back on; that EPISODE is
    bounded too — the counter restarts per episode, not per lifetime."""
    # Deadline 30 > the ~17 frames a static scene needs to calibrate
    # naturally, so warm-up must NOT trip a forced exit.
    md = MotionDetector((H, W), MotionConfig(calibration_max_frames=30))
    _warm_up(md)
    assert md.forced_calibration_exits == 0
    md.detect(_flat(255))                    # flash -> recalibrating
    assert md.is_calibrating() is True
    rng = np.random.default_rng(7)
    for _ in range(45):                      # stays busy after the flash
        md.detect(_busy_frame(rng))
    assert md.is_calibrating() is False
    assert md.forced_calibration_exits == 1


def test_skip_threshold_scene_counts_toward_the_deadline():
    """A scene permanently above skip_motion_threshold drops every
    frame AND used to wedge calibration — the dropped frames must count
    toward the deadline so the forced-exit counter (the operator
    signal) still ticks."""
    md = MotionDetector(
        (H, W),
        MotionConfig(skip_motion_threshold=0.2, calibration_max_frames=10),
    )
    rng = np.random.default_rng(3)
    for _ in range(40):
        frame = rng.integers(0, 255, (H, W), dtype=np.uint8)  # ~full-frame churn
        md.detect(frame)
    assert md.forced_calibration_exits >= 1


# ── Issue #373: env plumbing (DETECT_MOTION_*) ─────────────────────


def test_motion_config_from_env_defaults(monkeypatch):
    from detect_pipeline.service import motion_config_from_env
    for var in ("DETECT_MOTION_ENABLED", "DETECT_MOTION_THRESHOLD",
                "DETECT_MOTION_CONTOUR_AREA", "DETECT_MOTION_FRAME_ALPHA",
                "DETECT_MOTION_LIGHTNING_THRESHOLD",
                "DETECT_MOTION_CALIBRATION_MAX_FRAMES",
                "DETECT_MOTION_MAX_FORCED_EXITS"):
        monkeypatch.delenv(var, raising=False)
    cfg = motion_config_from_env()
    assert cfg == MotionConfig()  # env-less = library defaults, verbatim


def test_motion_config_from_env_overrides(monkeypatch):
    from detect_pipeline.service import motion_config_from_env
    monkeypatch.setenv("DETECT_MOTION_ENABLED", "false")
    monkeypatch.setenv("DETECT_MOTION_THRESHOLD", "45")
    monkeypatch.setenv("DETECT_MOTION_CONTOUR_AREA", "25")
    monkeypatch.setenv("DETECT_MOTION_FRAME_ALPHA", "0.05")
    monkeypatch.setenv("DETECT_MOTION_LIGHTNING_THRESHOLD", "0.95")
    monkeypatch.setenv("DETECT_MOTION_CALIBRATION_MAX_FRAMES", "600")
    monkeypatch.setenv("DETECT_MOTION_MAX_FORCED_EXITS", "5")
    cfg = motion_config_from_env()
    assert cfg.enabled is False
    assert cfg.threshold == 45
    assert cfg.contour_area == 25
    assert cfg.frame_alpha == 0.05
    assert cfg.lightning_threshold == 0.95
    assert cfg.calibration_max_frames == 600
    assert cfg.calibration_max_forced_exits == 5


def test_motion_config_from_env_rejects_junk(monkeypatch):
    from detect_pipeline.service import motion_config_from_env
    monkeypatch.setenv("DETECT_MOTION_ENABLED", "maybe")
    monkeypatch.setenv("DETECT_MOTION_THRESHOLD", "very")
    cfg = motion_config_from_env()
    assert cfg.enabled is True          # junk falls back to defaults
    assert cfg.threshold == 30


def test_worker_constructs_motion_from_env_with_camera_label():
    """Lockstep: the worker must build MotionDetector from
    motion_config_from_env() with the camera id as label — a hardcoded
    MotionConfig() here is #373 all over again."""
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1]
           / "detect_pipeline" / "service.py").read_text()
    assert "motion_config_from_env(), label=self.spec.camera_id" in src
    assert "MotionDetector((h, w), MotionConfig())" not in src


# ── The deadline as a DUTY CYCLE: latch open on an unmodelable scene ──
#
# #373 bounded each calibration episode, but on a scene with NO STATIC
# BACKGROUND (a PTZ mid-pan, a moving source) calibration re-trips the
# very next frame, so the deadline stops being an escape hatch and
# becomes a clock: one analysed frame every calibration_max_frames,
# forever, and quietly — the repeat WARN is demoted to debug. Measured
# on a real install: 6124/6220 frames skipped (98.5%), plate events
# arriving in exact 75 s multiples (150 frames / DETECT_FPS=2).


def _no_background_frame(rng: np.random.Generator) -> np.ndarray:
    """Every pixel churns: motion pct ~1.0, above the 0.8 lightning bar.

    This is the regime a moving camera produces — unlike ``_busy_frame``
    (pct ~0.3), it RE-TRIPS calibration on every frame, so the gate can
    never stay open and the deadline repeats indefinitely.
    """
    return rng.integers(0, 255, (H, W), dtype=np.uint8)


def test_repeated_forced_exits_latch_the_gate_open():
    """The fix: after N consecutive deadline exits the gate concludes
    the scene has no static background and stops re-gating it."""
    md = MotionDetector(
        (H, W),
        MotionConfig(calibration_max_frames=10, calibration_max_forced_exits=2),
        label="cam1",
    )
    rng = np.random.default_rng(11)
    for _ in range(100):
        md.detect(_no_background_frame(rng))
    assert md.latched_open is True
    assert md.is_calibrating() is False
    # The duty cycle is broken: no further forced exits accumulate,
    # because calibration is never re-entered.
    assert md.forced_calibration_exits == 2
    exits_at_latch = md.forced_calibration_exits
    for _ in range(100):
        md.detect(_no_background_frame(rng))
        assert md.is_calibrating() is False, "a latched gate must never re-gate"
    assert md.forced_calibration_exits == exits_at_latch


def test_latch_disabled_preserves_the_deadline_duty_cycle():
    """Regression guard for #373: with the latch off, behaviour is
    exactly as before — the deadline keeps firing forever."""
    md = MotionDetector(
        (H, W),
        MotionConfig(calibration_max_frames=10, calibration_max_forced_exits=0),
    )
    rng = np.random.default_rng(11)
    for _ in range(100):
        md.detect(_no_background_frame(rng))
    assert md.latched_open is False
    assert md.forced_calibration_exits > 2, "deadline should keep re-firing"


def test_latch_releases_when_the_scene_goes_quiet():
    """A PTZ that finishes its pan must start being gated again, with
    no operator action — the latch is an observation, not a setting."""
    md = MotionDetector(
        (H, W),
        # frame_alpha high so the background re-converges within the
        # test; release speed is a property of the background model,
        # not of the latch.
        MotionConfig(calibration_max_frames=10, calibration_max_forced_exits=2,
                     frame_alpha=0.5),
    )
    rng = np.random.default_rng(11)
    for _ in range(60):
        md.detect(_no_background_frame(rng))
    assert md.latched_open is True

    _warm_up(md, frames=120)                 # the pan stops; scene is static
    assert md.latched_open is False
    assert md.consecutive_forced_exits == 0
    assert md.is_calibrating() is False      # gated again, and calibrated


def test_natural_calibration_resets_the_forced_exit_counter():
    """Consecutive is the operative word: a scene that settles between
    episodes never accumulates its way to a latch."""
    md = MotionDetector(
        (H, W),
        MotionConfig(calibration_max_frames=10, calibration_max_forced_exits=2,
                     frame_alpha=0.5),
    )
    rng = np.random.default_rng(5)
    for _ in range(3):
        for _ in range(30):                  # unmodelable stretch -> one exit
            md.detect(_no_background_frame(rng))
        _warm_up(md, frames=120)             # ...then the scene settles
        assert md.consecutive_forced_exits == 0
    assert md.latched_open is False


def test_forced_exit_this_frame_is_a_per_frame_edge():
    """The metrics counter increments on this flag, so it must be true
    only on the frame the deadline fires — a sticky flag would inflate
    tier0_motion_forced_exits_total by one per frame thereafter."""
    md = MotionDetector(
        (H, W),
        MotionConfig(calibration_max_frames=10, calibration_max_forced_exits=0),
    )
    rng = np.random.default_rng(11)
    edges = 0
    for _ in range(100):
        md.detect(_no_background_frame(rng))
        if md.forced_exit_this_frame:
            edges += 1
    assert edges == md.forced_calibration_exits
