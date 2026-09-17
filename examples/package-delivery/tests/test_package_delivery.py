# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Orchestrator tests for PackageDelivery — the state machine
transitions (new → arrived → lingering → gone), severity routing
(owner pickup vs porch pirate), dedup, snapshot attachment, and
the config loader."""
from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from typing import Iterable
from unittest.mock import MagicMock

import pytest
from PIL import Image

from package_delivery import (
    EVENT_ARRIVED,
    EVENT_GONE_OWNER,
    EVENT_GONE_STRANGER,
    EVENT_LINGERING,
    AppConfig,
    CameraConfig,
    PackageDelivery,
    load_config,
)
from package_pipeline import Detection, FrameReads, Roi


def _frame_jpeg(width: int = 320, height: int = 240) -> bytes:
    img = Image.new("RGB", (width, height), (200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _reads(
    packages: Iterable[Detection] = (),
    persons: Iterable[Detection] = (),
    correlation_id: str = "cid-1",
) -> FrameReads:
    return FrameReads(
        packages=tuple(packages),
        persons=tuple(persons),
        frame_size=(320, 240),
        correlation_id=correlation_id,
    )


def _package(label: str = "suitcase", bbox=(50, 50, 150, 150),
             confidence: float = 0.9) -> Detection:
    return Detection(label=label, confidence=confidence, bbox=bbox)


def _person(bbox=(180, 50, 220, 220), confidence: float = 0.85) -> Detection:
    return Detection(label="person", confidence=confidence, bbox=bbox)


def _app_config(**overrides) -> AppConfig:
    base = AppConfig(
        kaic_url="http://localhost:8100",
        kaic_api_key="test-key",
        cameras=[CameraConfig(camera_id="front-porch", frame_url="http://example.invalid/snap.jpg")],
        poll_interval_seconds=0.0,
        request_timeout_seconds=1.0,
        arrive_consecutive_hits=2,
        gone_consecutive_misses=2,
        pickup_person_lookback_seconds=10.0,
        dedup_window_seconds=0.0,  # off by default so each transition fires
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _build(reads_per_call: Iterable[FrameReads | None], config: AppConfig | None = None):
    cfg = config or _app_config()
    pipeline = MagicMock()
    pipeline.process_frame.side_effect = list(reads_per_call)
    dispatcher = MagicMock()
    runtime = PackageDelivery(cfg, pipeline, dispatcher)

    class _StubFrameSource:
        def fetch(self) -> bytes:
            return _frame_jpeg()

    for cam_id in list(runtime._frame_sources):
        runtime._frame_sources[cam_id] = _StubFrameSource()

    return runtime, pipeline, dispatcher


# ── Config loader ──────────────────────────────────────────────────


def test_load_config_requires_kaic_url_and_api_key(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text("cameras:\n  - {camera_id: a, frame_url: http://x/a}\n")
    with pytest.raises(SystemExit, match="kaic_url"):
        load_config(cfg)


def test_load_config_parses_per_camera_roi(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n"
        "  - camera_id: a\n    frame_url: http://x/a\n"
        "    roi: [0.1, 0.2, 0.8, 0.9]\n"
    )
    parsed = load_config(cfg)
    assert len(parsed.cameras) == 1
    assert parsed.cameras[0].roi is not None


def test_load_config_rejects_malformed_roi(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n"
        "  - camera_id: a\n    frame_url: http://x/a\n"
        "    roi: [[0.0, 0.0], [1.0, 1.0]]\n"  # only 2 points
    )
    with pytest.raises(SystemExit, match="roi invalid"):
        load_config(cfg)


def test_load_config_rejects_zero_arrive_threshold(tmp_path: Path):
    """arrive_consecutive_hits=0 would let a single sighting fire
    arrival, defeating the anti-flicker intent. The loader must
    reject it with a clear error."""
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n  - {camera_id: a, frame_url: http://x/a}\n"
        "arrive_consecutive_hits: 0\n"
    )
    with pytest.raises(SystemExit, match="arrive_consecutive_hits"):
        load_config(cfg)


def test_load_config_rejects_non_numeric_confidence(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n  - {camera_id: a, frame_url: http://x/a}\n"
        "detection_confidence: high\n"
    )
    with pytest.raises(SystemExit, match="detection_confidence"):
        load_config(cfg)


def test_load_config_lowercases_label_lists(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n  - {camera_id: a, frame_url: http://x/a}\n"
        "package_labels: [Suitcase, BACKPACK]\n"
    )
    parsed = load_config(cfg)
    assert parsed.package_labels == ("suitcase", "backpack")


# ── State machine: arrival ─────────────────────────────────────────


def test_single_hit_does_not_fire_arrived():
    """arrive_consecutive_hits=2 by default — a single sighting must
    not produce an alert. Reduces flicker."""
    runtime, _pipeline, dispatcher = _build([_reads(packages=[_package()])])
    runtime.step()
    dispatcher.dispatch.assert_not_called()


def test_two_consecutive_hits_fire_arrived():
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),
    ])
    runtime.step()
    runtime.step()
    assert dispatcher.dispatch.call_count == 1
    alert = dispatcher.dispatch.call_args.args[0]
    assert alert.severity == "info"
    assert "arrived" in alert.title.lower()
    assert alert.evidence["event_kind"] == EVENT_ARRIVED


def test_higher_threshold_delays_arrival():
    cfg = _app_config(arrive_consecutive_hits=4)
    runtime, _pipeline, dispatcher = _build(
        [_reads(packages=[_package()]) for _ in range(4)], config=cfg,
    )
    for _ in range(3):
        runtime.step()
    dispatcher.dispatch.assert_not_called()
    runtime.step()
    assert dispatcher.dispatch.call_count == 1


# ── State machine: gone (owner vs stranger) ────────────────────────


def test_gone_with_recent_person_fires_owner_pickup():
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()], persons=[_person()]),
        _reads(),  # miss 1
        _reads(),  # miss 2 → "gone"
    ])
    for _ in range(4):
        runtime.step()
    kinds = [c.args[0].evidence["event_kind"] for c in dispatcher.dispatch.call_args_list]
    assert EVENT_ARRIVED in kinds
    assert EVENT_GONE_OWNER in kinds
    # Owner pickup is info severity.
    pickup = next(
        c.args[0] for c in dispatcher.dispatch.call_args_list
        if c.args[0].evidence["event_kind"] == EVENT_GONE_OWNER
    )
    assert pickup.severity == "info"


def test_gone_without_person_fires_stranger_alert():
    """No person sighting in the lookback window → porch-pirate severity."""
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),  # arrival fires here
        _reads(),  # miss 1
        _reads(),  # miss 2 → "gone"
    ])
    for _ in range(4):
        runtime.step()
    stranger = next(
        c.args[0] for c in dispatcher.dispatch.call_args_list
        if c.args[0].evidence["event_kind"] == EVENT_GONE_STRANGER
    )
    assert stranger.severity == "high"


def test_pickup_lookback_zero_disables_heuristic():
    """lookback=0 means 'don't try to classify owner-vs-stranger' —
    every disappearance fires as an owner pickup (info severity) so
    homelab users aren't woken by porch-pirate alerts every time they
    bring in a package themselves."""
    cfg = _app_config(pickup_person_lookback_seconds=0.0)
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),  # arrival fires here
        _reads(),  # miss 1
        _reads(),  # miss 2 → "gone"
    ], config=cfg)
    for _ in range(4):
        runtime.step()
    gone = [
        c.args[0] for c in dispatcher.dispatch.call_args_list
        if c.args[0].evidence["event_kind"] != EVENT_ARRIVED
    ]
    assert len(gone) == 1
    assert gone[0].evidence["event_kind"] == EVENT_GONE_OWNER
    assert gone[0].severity == "info"


def test_dedup_suppressed_gone_keeps_track_for_retry():
    """When dedup suppresses a 'gone' attempt, the track must NOT be
    dropped — keeping it around lets a later un-suppressed firing
    happen and/or a sudden reappearance rematch the existing track
    instead of spawning a fresh trk_id. This is the H1 invariant
    from peer review."""
    cfg = _app_config(
        dedup_window_seconds=60.0,
        pickup_person_lookback_seconds=0.0,
    )
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),
        _reads(),
        _reads(),  # would fire gone (first time, dispatches)
    ], config=cfg)
    for _ in range(4):
        runtime.step()
    tracker = runtime._trackers["front-porch"]
    # First gone fired AND dispatched, so the track should be dropped.
    assert tracker.tracks == {}

    # Now run a separate scenario where the gone attempt is dedup-
    # suppressed by pre-seeding _last_fired.
    cfg2 = _app_config(
        dedup_window_seconds=60.0,
        pickup_person_lookback_seconds=0.0,
    )
    runtime2, _pipeline2, dispatcher2 = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),  # arrival fires; trk_000001 created
        _reads(),
        _reads(),  # gone attempt — but dedup will suppress it
    ], config=cfg2)
    # Pre-seed the dedup window for the gone event so the dispatch
    # is suppressed.
    runtime2._last_fired[("front-porch", "trk_000001", EVENT_GONE_OWNER)] = (
        time.monotonic() - 1.0
    )
    for _ in range(4):
        runtime2.step()
    # Track must still exist — dedup-suppressed gone keeps it alive.
    assert "trk_000001" in runtime2._trackers["front-porch"].tracks
    # And the gone event should NOT have fired (only arrival).
    gone = [
        c for c in dispatcher2.dispatch.call_args_list
        if c.args[0].evidence["event_kind"] in (
            EVENT_GONE_OWNER, EVENT_GONE_STRANGER
        )
    ]
    assert gone == []
    # State stays at "arrived" because dispatch was suppressed.
    assert runtime2._trackers["front-porch"].tracks["trk_000001"].state == "arrived"


def test_flicker_track_below_arrival_threshold_does_not_fire_gone():
    """A track that was seen once, then missed forever, must NOT fire
    a 'gone' alert — it never crossed the arrival threshold so the
    operator was never told it existed."""
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(),  # miss 1
        _reads(),  # miss 2 → would drop the track
    ])
    for _ in range(3):
        runtime.step()
    dispatcher.dispatch.assert_not_called()


# ── Linger alert ──────────────────────────────────────────────────


def test_linger_alert_fires_after_threshold(monkeypatch):
    """Force a long wall-clock between hits so the linger threshold
    is crossed deterministically."""
    cfg = _app_config(
        linger_alert_after_seconds=5.0,
        arrive_consecutive_hits=2,
    )
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),  # arrives at t≈t1
        _reads(packages=[_package()]),  # later — should fire linger
    ], config=cfg)

    # Drive time so the third tick lands well past the linger threshold.
    fake_now = iter([100.0, 100.5, 110.0])

    import time
    monkeypatch.setattr(time, "monotonic", lambda: next(fake_now))

    for _ in range(3):
        runtime.step()

    kinds = [c.args[0].evidence["event_kind"] for c in dispatcher.dispatch.call_args_list]
    assert EVENT_ARRIVED in kinds
    assert EVENT_LINGERING in kinds


def test_linger_alert_disabled_when_zero():
    cfg = _app_config(linger_alert_after_seconds=0.0)
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),
    ], config=cfg)
    for _ in range(3):
        runtime.step()
    kinds = [c.args[0].evidence["event_kind"] for c in dispatcher.dispatch.call_args_list]
    assert EVENT_LINGERING not in kinds


# ── No-detection handling ─────────────────────────────────────────


def test_pipeline_returning_none_does_not_fire():
    runtime, _pipeline, dispatcher = _build([None])
    runtime.step()
    dispatcher.dispatch.assert_not_called()


def test_empty_reads_do_not_fire():
    runtime, _pipeline, dispatcher = _build([_reads()])
    runtime.step()
    dispatcher.dispatch.assert_not_called()


# ── Snapshot attachment ───────────────────────────────────────────


def test_arrival_alert_carries_snapshot_when_enabled():
    cfg = _app_config(attach_snapshot_on_alerts=True)
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),
    ], config=cfg)
    runtime.step()
    runtime.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert "snapshot_b64" in alert.evidence
    base64.b64decode(alert.evidence["snapshot_b64"])  # round-trip valid


def test_arrival_alert_omits_snapshot_when_disabled():
    cfg = _app_config(attach_snapshot_on_alerts=False)
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),
    ], config=cfg)
    runtime.step()
    runtime.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert "snapshot_b64" not in alert.evidence


def test_oversized_snapshot_dropped_with_warning(caplog):
    cfg = _app_config(attach_snapshot_on_alerts=True, snapshot_max_bytes=3)
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),
    ], config=cfg)
    with caplog.at_level("WARNING", logger="package-delivery"):
        runtime.step()
        runtime.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert "snapshot_b64" not in alert.evidence
    assert any(
        "exceeds snapshot_max_bytes" in rec.getMessage() for rec in caplog.records
    )


# ── Dedup ──────────────────────────────────────────────────────────


def test_dedup_does_not_suppress_when_track_is_dropped_and_recreated():
    """A real new arrival (after the previous track was dropped
    post-gone) gets a fresh track_id, so the dedup key changes and
    the second arrival fires normally. This protects the operator
    against the failure mode where they'd miss a genuine re-delivery
    just because it happened within the dedup window."""
    cfg = _app_config(dedup_window_seconds=60.0)
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),  # arrival 1 fires (trk_000001)
        _reads(),
        _reads(),  # gone fires; track dropped
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),  # arrival 2 fires (trk_000002)
    ], config=cfg)
    for _ in range(6):
        runtime.step()
    arrivals = [
        c for c in dispatcher.dispatch.call_args_list
        if c.args[0].evidence["event_kind"] == EVENT_ARRIVED
    ]
    assert len(arrivals) == 2


# ── Evidence payload ──────────────────────────────────────────────


def test_evidence_carries_track_metadata():
    runtime, _pipeline, dispatcher = _build([
        _reads(packages=[_package()]),
        _reads(packages=[_package()]),
    ])
    runtime.step()
    runtime.step()
    alert = dispatcher.dispatch.call_args.args[0]
    e = alert.evidence
    assert e["event_kind"] == EVENT_ARRIVED
    assert e["label"] == "suitcase"
    assert "track_id" in e
    assert e["track_id"].startswith("trk_")
    assert e["hits"] >= 2
    assert e["bbox"] == [50, 50, 150, 150]
    assert alert.tags == [EVENT_ARRIVED]


# ── Connected: cameras picked in the catalog ───────────────────────


def _connected(reads_per_call, picked):
    runtime, pipeline, dispatcher = _build(reads_per_call, _app_config(cameras=[]))
    runtime._config_poll_thread = object()   # picks arrive on the config poll

    class _Core:
        def get_frame(self, camera_id):
            return _frame_jpeg()

    runtime._source._core = _Core()
    runtime.on_cameras_update(frozenset(picked))
    return runtime, pipeline, dispatcher


def test_a_picked_camera_is_polled_through_core_with_the_drawn_roi():
    runtime, pipeline, _ = _connected([_reads()], {3})
    runtime.on_config_update({"roi": {"3": [0.1, 0.2, 0.8, 0.9]}})
    runtime.step()
    assert pipeline.process_frame.call_count == 1
    assert pipeline.process_frame.call_args.kwargs["roi"] is not None


def test_nothing_picked_polls_nothing():
    runtime, pipeline, _ = _connected([_reads()], set())
    runtime.step()
    assert pipeline.process_frame.call_count == 0


def test_a_picked_camera_remembers_who_was_on_the_porch():
    """A picked camera has no per-camera state until its first frame. The
    person-sighting buffer must be created then, not appended to a
    throwaway list — or every pickup reads as a stranger's."""
    runtime, _, _ = _connected([_reads(persons=[_person()])], {3})
    runtime.step()
    assert len(runtime._person_sightings.get("cam3", [])) == 1
