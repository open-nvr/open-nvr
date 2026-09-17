# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Pure-handler tests for the loitering-detection state machine.

The NATS subscribe loop is exercised by the inference-listener
example's tests; here we focus on the only piece this app uniquely
contributes — the per-(camera, label) dwell state machine.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from alerts import Alert, AlertDispatcher
from loitering_detection import (
    AppConfig,
    CameraWatch,
    LoiteringDetector,
    load_config,
)
from zone import Zone


# ── Helpers ────────────────────────────────────────────────────────


def _make_event(
    *,
    camera_id: str,
    completed_at: str,
    label: str = "person",
    in_zone: bool = True,
    correlation_id: str = "corr-1",
    adapter: str = "yolov8",
) -> dict[str, Any]:
    """Build a fake ``InferenceCompletedEvent`` body. The center
    zone in the test config is [480, 270] - [1440, 810] on a
    1920x1080 frame, so bbox=(0.45, 0.45, 0.1, 0.1) → center
    (1000, 580) which IS in the center zone, and (0.05, 0.05, 0.1, 0.1)
    → center (192, 108) which is NOT."""
    if in_zone:
        bbox = {"x": 0.45, "y": 0.45, "w": 0.1, "h": 0.1}
    else:
        bbox = {"x": 0.01, "y": 0.01, "w": 0.05, "h": 0.05}
    return {
        "correlation_id": correlation_id,
        "adapter": adapter,
        "adapter_version": "1.0.0",
        "camera_id": camera_id,
        "model_name": "yolov8n",
        "model_version": "v1",
        "model_fingerprint": "sha256:test",
        "inference_ms": 12,
        "completed_at": completed_at,
        "result": {
            "detections": [{
                "label": label, "confidence": 0.9, "bbox": bbox,
                "track_id": None, "attributes": {},
            }],
            "frame_dimensions": {"w": 1920, "h": 1080},
        },
    }


def _ts(seconds_after_epoch_base: float, *, base: float = 1_700_000_000.0) -> str:
    """ISO timestamp at ``base + seconds_after_epoch_base``. Lets
    tests reason about dwell in real seconds."""
    dt = _dt.datetime.fromtimestamp(base + seconds_after_epoch_base, _dt.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


class _RecorderChannel:
    name = "recorder"

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert):
        self.alerts.append(alert)
        return True


def _build_detector(
    *,
    threshold_seconds: float = 30.0,
    grace_period_seconds: float = 5.0,
    watch_labels: list[str] | None = None,
) -> tuple[LoiteringDetector, _RecorderChannel]:
    """Build a LoiteringDetector with one camera + center-zone config."""
    zone = Zone.from_config("center", [[480, 270], [1440, 270], [1440, 810], [480, 810]])
    camera = CameraWatch(
        camera_id="cam-test",
        zone=zone,
        frame_width=1920,
        frame_height=1080,
    )
    config = AppConfig(
        nats_url="nats://test:4222",
        nats_token=None,
        subject_pattern="opennvr.inference.>",
        watch_labels=watch_labels or ["person", "car"],
        threshold_seconds=threshold_seconds,
        grace_period_seconds=grace_period_seconds,
        cameras={"cam-test": camera},
        webhook_url=None,
    )
    recorder = _RecorderChannel()
    dispatcher = AlertDispatcher([recorder])
    detector = LoiteringDetector(config, dispatcher)
    return detector, recorder


# ── State-machine tests ───────────────────────────────────────────


def test_single_in_zone_event_does_not_fire():
    """One frame inside the zone is not loitering — it's just being
    there. No alert until threshold crossed."""
    detector, recorder = _build_detector(threshold_seconds=30.0)
    fired = detector.handle_event(_make_event(
        camera_id="cam-test", completed_at=_ts(0),
    ))
    assert fired == []
    assert recorder.alerts == []


def test_continuous_presence_below_threshold_no_alert():
    """Continuous presence (every 1s frame, person always in zone)
    for less than threshold doesn't fire."""
    detector, recorder = _build_detector(threshold_seconds=30.0, grace_period_seconds=5.0)
    for t in range(0, 21):
        detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(t)))
    assert recorder.alerts == []


def test_continuous_presence_crossing_threshold_fires_once():
    """First frame where dwell ≥ threshold fires; subsequent frames
    in the same episode don't re-fire."""
    detector, recorder = _build_detector(threshold_seconds=30.0, grace_period_seconds=5.0)
    # 1fps frames 0..29 with person always in zone
    for t in range(0, 30):
        detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(t)))
    # Threshold crossed at t=30
    fired_at_30 = detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(30)))
    assert len(fired_at_30) == 1
    assert "person" in fired_at_30[0].title.lower()
    assert "30" in fired_at_30[0].description or "30.0" in fired_at_30[0].description
    # Subsequent frame at t=31 — same dwell episode, NO new alert
    fired_at_31 = detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(31)))
    assert fired_at_31 == []
    assert len(recorder.alerts) == 1


def test_absence_beyond_grace_resets_dwell():
    """Person enters, leaves, comes back. The 2nd arrival is a FRESH
    dwell — must NOT count time from the 1st arrival. Models a
    typical "two people walk through" not "one person stayed."

    Encoded as: 1fps frames where in_zone toggles. ``in_zone=False``
    frames are the absent signal — they're what triggers
    ``_gc_absent_labels`` to reset state when the gap > grace_period.
    """
    detector, recorder = _build_detector(threshold_seconds=30.0, grace_period_seconds=5.0)
    # First arrival, brief presence (t=0..2)
    for t in (0, 1, 2):
        detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(t), in_zone=True))
    # Absent frames (t=3..10) → state is GC'd after grace=5s elapses
    for t in (3, 4, 5, 6, 7, 8, 9, 10):
        detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(t), in_zone=False))
    # Fresh arrival starts at t=11
    for t in range(11, 31):
        detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(t), in_zone=True))
    # At t=31, the FRESH dwell is only 20s (11..31) — below threshold
    fired = detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(31), in_zone=True))
    assert fired == []
    assert recorder.alerts == []


def test_out_of_zone_detections_dont_count():
    """Watched-label detections OUTSIDE the zone don't contribute
    to the dwell timer for that camera."""
    detector, recorder = _build_detector(threshold_seconds=10.0)
    for t in (0, 5, 10, 15, 20, 25):
        detector.handle_event(_make_event(
            camera_id="cam-test", completed_at=_ts(t), in_zone=False,
        ))
    assert recorder.alerts == []


def test_non_watched_label_doesnt_count():
    """A 'bike' in the zone (not watched) doesn't trigger the
    dwell timer."""
    detector, recorder = _build_detector(
        threshold_seconds=10.0, watch_labels=["person"],
    )
    for t in (0, 5, 10, 15):
        detector.handle_event(_make_event(
            camera_id="cam-test", completed_at=_ts(t), label="bike",
        ))
    assert recorder.alerts == []


def test_unknown_camera_id_ignored():
    """An event for a camera not in our config is silently dropped —
    other monitoring apps may be watching it, but we're not."""
    detector, recorder = _build_detector()
    fired = detector.handle_event(_make_event(
        camera_id="cam-not-configured", completed_at=_ts(0),
    ))
    assert fired == []
    assert recorder.alerts == []


def test_per_label_state_is_independent():
    """A person and a car loitering simultaneously fire SEPARATE
    alerts — per-(camera, label) state, not per-camera."""
    detector, recorder = _build_detector(
        threshold_seconds=10.0, watch_labels=["person", "car"],
    )
    # Both labels present from t=0..10 at 1fps
    for t in range(0, 11):
        detector.handle_event(_make_event(
            camera_id="cam-test", completed_at=_ts(t), label="person",
        ))
        detector.handle_event(_make_event(
            camera_id="cam-test", completed_at=_ts(t), label="car",
        ))
    # Two alerts — one per label, both fired at the t=10 frame
    assert len(recorder.alerts) == 2
    labels_in_alerts = sorted(a.tags[-1] for a in recorder.alerts)
    assert labels_in_alerts == ["car", "person"]


def test_alert_carries_correlation_id_from_event():
    """The alert must reference the same correlation_id KAI-C
    audited, so an operator investigating an alert can find it
    in the audit chain."""
    detector, recorder = _build_detector(threshold_seconds=10.0, grace_period_seconds=5.0)
    # 1fps presence frames; the threshold-crossing event carries
    # the correlation_id we'll match against.
    for t in range(0, 10):
        detector.handle_event(_make_event(
            camera_id="cam-test", completed_at=_ts(t),
            correlation_id="my-trace-id",
        ))
    fired = detector.handle_event(_make_event(
        camera_id="cam-test", completed_at=_ts(10),
        correlation_id="my-trace-id",
    ))
    assert len(fired) == 1
    assert fired[0].correlation_id == "my-trace-id"


def test_alert_evidence_includes_model_fingerprint():
    """For §11.3 audit-chain joining, the alert's evidence body
    must include the model_fingerprint from the event so an
    operator can verify the inference was produced by the
    expected weights."""
    detector, recorder = _build_detector(threshold_seconds=10.0, grace_period_seconds=5.0)
    for t in range(0, 11):
        detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(t)))
    assert len(recorder.alerts) == 1
    assert recorder.alerts[0].evidence["model_fingerprint"] == "sha256:test"
    assert recorder.alerts[0].evidence["adapter"] == "yolov8"


def test_malformed_event_does_not_crash():
    """Defense in depth — a non-dict, missing fields, or weird types
    should not crash the handler. The detector is a long-lived
    process; one bad event shouldn't take it down."""
    detector, recorder = _build_detector()
    # None / non-dict
    assert detector.handle_event(None) == []  # type: ignore[arg-type]
    assert detector.handle_event("not a dict") == []  # type: ignore[arg-type]
    # Missing camera_id
    assert detector.handle_event({"result": {"detections": []}}) == []
    # Missing result
    assert detector.handle_event({"camera_id": "cam-test"}) == []
    # Detections is not a list
    assert detector.handle_event({
        "camera_id": "cam-test",
        "completed_at": _ts(0),
        "result": {"detections": "not a list"},
    }) == []
    assert recorder.alerts == []


# ── Config tests ──────────────────────────────────────────────────


def test_load_config_minimal(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        nats_url: "nats://nats:4222"
        threshold_seconds: 60
        grace_period_seconds: 5
        cameras:
          - camera_id: "c1"
            zone_name: "Z1"
            zone: [[0,0],[100,0],[100,100],[0,100]]
    """))
    c = load_config(str(cfg))
    assert c.nats_url == "nats://nats:4222"
    assert c.threshold_seconds == 60
    assert c.grace_period_seconds == 5
    assert "c1" in c.cameras
    assert c.cameras["c1"].zone.name == "Z1"


def test_load_config_rejects_non_positive_threshold(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        nats_url: "nats://nats:4222"
        threshold_seconds: 0
        cameras:
          - camera_id: "c1"
            zone_name: "Z1"
            zone: [[0,0],[100,0],[100,100],[0,100]]
    """))
    with pytest.raises(ValueError, match="threshold_seconds"):
        load_config(str(cfg))


def test_load_config_rejects_no_cameras(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        nats_url: "nats://nats:4222"
        threshold_seconds: 60
        grace_period_seconds: 5
    """))
    with pytest.raises(ValueError, match="camera"):
        load_config(str(cfg))


def test_load_config_defaults_watch_labels(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        nats_url: "nats://nats:4222"
        threshold_seconds: 30
        grace_period_seconds: 3
        cameras:
          - camera_id: "c1"
            zone: [[0,0],[100,0],[100,100],[0,100]]
    """))
    c = load_config(str(cfg))
    assert c.watch_labels == ["person"]  # default


def test_load_config_rejects_duplicate_camera_id(tmp_path: Path):
    """Regression for peer-review H1: two camera entries with the
    same id silently overwrote each other (second wins). Refuse at
    validate time so operator intent isn't lost."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        nats_url: "nats://nats:4222"
        threshold_seconds: 30
        grace_period_seconds: 5
        cameras:
          - camera_id: "shared"
            zone: [[0,0],[100,0],[100,100],[0,100]]
          - camera_id: "shared"
            zone: [[200,200],[300,200],[300,300],[200,300]]
    """))
    with pytest.raises(ValueError, match="duplicate"):
        load_config(str(cfg))


def test_load_config_rejects_empty_watch_labels(tmp_path: Path):
    """Regression for peer-review H2: an explicit empty
    ``watch_labels: []`` previously produced a detector that
    silently matched nothing. Refuse at validate time. Omitting
    the key entirely still gives the ``['person']`` default."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        nats_url: "nats://nats:4222"
        threshold_seconds: 30
        grace_period_seconds: 5
        watch_labels: []
        cameras:
          - camera_id: "c1"
            zone: [[0,0],[100,0],[100,100],[0,100]]
    """))
    with pytest.raises(ValueError, match="watch_labels"):
        load_config(str(cfg))


def test_load_config_rejects_zero_frame_width(tmp_path: Path):
    """Regression for peer-review H3: frame_width/height ≤ 0 silently
    bucketed every detection at (0,0) and missed every zone check.
    Refuse rather than swallow."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        nats_url: "nats://nats:4222"
        threshold_seconds: 30
        grace_period_seconds: 5
        cameras:
          - camera_id: "c1"
            frame_width: 0
            frame_height: 1080
            zone: [[0,0],[100,0],[100,100],[0,100]]
    """))
    with pytest.raises(ValueError, match="frame_width"):
        load_config(str(cfg))


def test_handle_event_skips_out_of_order_events():
    """Regression for peer-review M2: an event with ``completed_at``
    older than the most recent one we've already processed must be
    skipped — dwell math assumes monotonic time, and a backward jump
    would silently corrupt state. NATS doesn't guarantee strict
    ordering across publishers."""
    detector, recorder = _build_detector(threshold_seconds=10.0, grace_period_seconds=5.0)
    # Establish state at t=10
    detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(10)))
    state_after_t10 = detector._states[("cam-test", "person")]
    last_seen_t10 = state_after_t10.last_seen
    present_since_t10 = state_after_t10.present_since
    # Out-of-order event at t=5 (older) — must be skipped, state unchanged
    fired = detector.handle_event(_make_event(camera_id="cam-test", completed_at=_ts(5)))
    assert fired == []
    state_after_skip = detector._states[("cam-test", "person")]
    assert state_after_skip.last_seen == last_seen_t10
    assert state_after_skip.present_since == present_since_t10
    assert recorder.alerts == []


# ── Connected: cameras picked in the catalog, zones drawn there ─────


def _connected_detector(picked, **kw):
    """A detector with NO YAML cameras, running as it does against core:
    the config poll is live and has delivered picks."""
    detector, recorder = _build_detector(threshold_seconds=10.0, **kw)
    detector.cfg.cameras = {}
    detector._config_poll_thread = object()
    detector.picked_cameras = frozenset(picked)
    return detector, recorder


def _dwell(detector, camera_id, *, in_zone=True):
    fired = []
    for t in (0, 5, 11):
        fired += detector.handle_event(_make_event(
            camera_id=camera_id, completed_at=_ts(t), in_zone=in_zone))
    return fired


def test_a_picked_camera_is_watched_with_no_yaml_entry():
    """Nothing drawn yet: the whole frame is the zone."""
    detector, _ = _connected_detector({3})
    assert len(_dwell(detector, "cam3", in_zone=False)) == 1


def test_an_unpicked_camera_is_ignored():
    detector, _ = _connected_detector({3})
    assert _dwell(detector, "cam4") == []


def test_the_zone_drawn_in_the_catalog_applies():
    """Saved by the zone editor under the numeric id, in 0-1 coordinates."""
    detector, _ = _connected_detector({3})
    detector.on_config_update({"zones": {"3": [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]}})
    assert _dwell(detector, "cam3", in_zone=False) == []
    assert len(_dwell(detector, "cam3", in_zone=True)) == 1


def test_config_needs_cameras_only_when_standalone(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text('nats_url: "nats://x:4222"\nopennvr_url: "http://core:8000"\n')
    assert load_config(str(p)).cameras == {}
    p.write_text('nats_url: "nats://x:4222"\n')
    with pytest.raises(ValueError, match="at least one camera"):
        load_config(str(p))
