# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-camera tuning: core's ``detection`` record overrides the global
knobs for one camera, per-label object filters drop the wrong-sized, and a
changed record restarts the worker."""
from __future__ import annotations

from detect_pipeline.detector import RawDetection
from detect_pipeline.ffmpeg_presets import frame_size_bytes
from detect_pipeline.frame_source import Frame
from detect_pipeline.motion import MotionConfig
from detect_pipeline.pipeline import DetectPipeline
from detect_pipeline.providers import _to_spec
from detect_pipeline.service import CameraSpec, WorkerManager
from detect_pipeline.tracking import Detection, TrackConfig, Tracker
from detect_pipeline.tuning import ObjectFilter, ObjectSizeFilter, Tuning, parse_tuning


# ── parsing ──────────────────────────────────────────────────────────

def test_absent_or_junk_means_inherit_everything():
    for raw in (None, {}, "x", [], {"fps": "fast"}, {"fps": 0}, {"fps": 31},
                {"motion_threshold": 300}, {"motion_enabled": "maybe"},
                {"object_filters": "x"}, {"object_filters": {"car": "x"}},
                {"object_filters": {"car": {}}}, {"fps": True}):
        assert parse_tuning(raw) == Tuning(), raw
    assert parse_tuning(None).is_default


def test_fields_are_parsed_and_ranged():
    t = parse_tuning({
        "fps": 7.0, "motion_enabled": False, "motion_threshold": "40",
        "motion_contour_area": 25, "min_spawn_score": 0.7, "stationary_interval": 0,
        "object_filters": {"Person": {"min_area": 0.001, "max_area": 0.5, "min_score": 0.6},
                           "car": {"min_ratio": 0.8, "max_ratio": 4, "bogus": 1},
                           "dog": {"min_area": 5}},     # out of range → no limit → dropped
    })
    assert t == Tuning(
        fps=7, motion_enabled=False, motion_threshold=40, motion_contour_area=25,
        min_spawn_score=0.7, stationary_interval=0,
        object_filters=(
            ("car", ObjectFilter(min_ratio=0.8, max_ratio=4.0)),
            ("person", ObjectFilter(min_area=0.001, max_area=0.5, min_score=0.6)),
        ),
    )
    assert not t.is_default
    assert "fps=7" in t.describe() and "object_filters=car,person" in t.describe()
    hash(t)


def test_motion_config_overrides_only_what_is_set():
    base = MotionConfig(threshold=30, contour_area=10, enabled=True, frame_alpha=0.02)
    assert Tuning().motion_config(base) is base
    out = Tuning(motion_threshold=50, motion_enabled=False).motion_config(base)
    assert (out.threshold, out.contour_area, out.enabled, out.frame_alpha) == (50, 10, False, 0.02)


# ── the filter ───────────────────────────────────────────────────────

def test_object_filter_judges_area_ratio_and_score():
    f = ObjectSizeFilter({"person": ObjectFilter(min_area=0.01, max_area=0.5,
                                                 min_ratio=0.2, max_ratio=1.0, min_score=0.5)},
                         (100, 100))
    ok = Detection("person", (10, 10, 40, 60), 0.9)          # 1500 px = 15 %, ratio 0.6
    tiny = Detection("person", (10, 10, 15, 15), 0.9)         # 25 px = 0.25 %
    huge = Detection("person", (0, 0, 90, 90), 0.9)           # 81 %
    wide = Detection("person", (0, 0, 60, 20), 0.9)           # ratio 3
    weak = Detection("person", (10, 10, 40, 60), 0.3)
    other = Detection("car", (10, 10, 15, 15), 0.3)           # no filter for cars
    assert f([ok, tiny, huge, wide, weak, other]) == [ok, other]
    assert f.dropped == 4
    assert f.reason("person", (10, 10, 15, 15), 0.9) == "min_area"
    assert f.reason("person", (0, 0, 60, 20), 0.9) == "max_ratio"
    assert f.reason("PERSON", (10, 10, 40, 60), 0.3) == "min_score"
    assert f.reason("person", (10, 10, 40, 10), 0.9) == "min_area", "a zero-height box is tiny"


def test_pipeline_applies_the_filter_after_nms_and_counts_it():
    W, H = 320, 240

    class _Motion:
        def detect(self, luma):
            return [(100, 80, 160, 200)]

        def is_calibrating(self):
            return False

    class _OneBox:
        def detect(self, crop):
            return [RawDetection("person", 0.9, (0.25, 0.25, 0.75, 0.75))]

    frame = Frame(bytes(frame_size_bytes(W, H)), W, H, 0, 0.0)
    for filters, expect_dets, expect_filtered in (
        ({"person": ObjectFilter(max_area=0.001)}, 0, 1),    # everything is "too big"
        ({"person": ObjectFilter(min_area=0.001)}, 1, 0),
        ({"car": ObjectFilter(max_area=0.001)}, 1, 0),       # other label: untouched
    ):
        tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
        pipe = DetectPipeline(None, _Motion(), _OneBox(), tracker,
                              object_filter=ObjectSizeFilter(filters, (H, W)))
        result = pipe.process_frame(frame)
        assert len(result.detections) == expect_dets, filters
        assert result.filtered_detections == expect_filtered, filters


# ── the spec ─────────────────────────────────────────────────────────

def _cam(**extra):
    return {"camera_id": "cam9", "open_nvr_camera_id": "9", "name": "n",
            "frame_url": "rtsp://m/cam-9?jwt=a", **extra}


def test_the_record_lands_on_the_spec_and_its_fps_wins(monkeypatch):
    monkeypatch.setenv("DETECT_FPS", "2")
    assert _to_spec(_cam()).tuning == Tuning() and _to_spec(_cam()).fps == 2
    spec = _to_spec(_cam(detection={"fps": 6, "motion_threshold": 45,
                                    "object_filters": {"car": {"min_area": 0.01}}}))
    assert spec.fps == 6
    assert spec.tuning.motion_threshold == 45
    assert spec.tuning.filters() == {"car": ObjectFilter(min_area=0.01)}
    # Without a per-camera fps the listing's own fps still applies.
    assert _to_spec(_cam(fps=4, detection={"motion_threshold": 45})).fps == 4


def test_a_changed_record_is_a_restart_but_a_new_token_is_not():
    a = CameraSpec("cam9", "n", "rtsp://m/cam-9?jwt=a", tuning=Tuning(fps=6))
    same = CameraSpec("cam9", "n", "rtsp://m/cam-9?jwt=b", tuning=Tuning(fps=6))
    changed = CameraSpec("cam9", "n", "rtsp://m/cam-9?jwt=b", tuning=Tuning(fps=6, motion_threshold=45))
    assert WorkerManager._baked(a) == WorkerManager._baked(same)
    assert WorkerManager._baked(a) != WorkerManager._baked(changed)
    assert WorkerManager._baked(a) != WorkerManager._baked(CameraSpec("cam9", "n", "rtsp://m/cam-9?jwt=b"))
