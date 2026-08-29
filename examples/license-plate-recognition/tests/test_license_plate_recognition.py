# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the pure-consumer PlateAlerter (RFC-0002 Phase 4).

The app's whole job now: consume ``plate.recognized.v1`` envelopes,
route severity through the watchlists, dedup per (camera, plate),
scope to assigned cameras, deliver alerts. Everything here runs
without NATS, KAI-C, or core — envelopes go straight through
``handle_event``.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import license_plate_recognition as lpr
from license_plate_recognition import (
    AppConfig,
    PLATE_SUBJECT_PATTERN,
    PlateAlerter,
    load_config,
)


def _config(**overrides) -> AppConfig:
    base = AppConfig(nats_url="nats://test:4222")
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _alerter(**overrides) -> tuple[PlateAlerter, MagicMock]:
    dispatcher = MagicMock()
    return PlateAlerter(_config(**overrides), dispatcher), dispatcher


def _envelope(plate="ABC1234", camera="cam-1", *, confidence=0.9,
              vehicle="car", schema="plate.recognized.v1", **extra):
    env = {
        "id": "evt_0123456789ab",
        "schema": schema,
        "correlation_id": "corr-1",
        "camera_id": camera,
        "ts": "2026-08-29T10:00:00+00:00",
        "producer": "kai-c",
        "payload": {
            "plate_text": plate,
            "confidence": confidence,
            "vehicle_label": vehicle,
            "event_id": 42,
        },
    }
    env.update(extra)
    return env


# ── Severity routing ────────────────────────────────────────────────


def test_unlisted_plate_fires_info_read():
    alerter, dispatcher = _alerter()
    fired = alerter.handle_event(_envelope())
    assert len(fired) == 1 and fired[0].severity == "info"
    assert "ABC1234" in fired[0].title
    assert dispatcher.fire.call_count == 1
    assert fired[0].evidence["vehicle_label"] == "car"
    assert fired[0].correlation_id == "corr-1"


def test_denylist_plate_fires_high():
    alerter, _ = _alerter(denylist=["BAD001"])
    fired = alerter.handle_event(_envelope(plate="bad 001"))
    assert fired[0].severity == "high"
    assert fired[0].evidence["in_denylist"] is True


def test_allowlist_plate_fires_low():
    alerter, _ = _alerter(allowlist=["OK123"])
    fired = alerter.handle_event(_envelope(plate="ok123"))
    assert fired[0].severity == "low"


# ── Dedup ledger ────────────────────────────────────────────────────


def test_dedup_suppresses_within_window_per_camera():
    alerter, dispatcher = _alerter(dedup_window_seconds=60.0)
    assert len(alerter.handle_event(_envelope())) == 1
    assert alerter.handle_event(_envelope()) == []          # suppressed
    # Same plate on ANOTHER camera is its own ledger entry.
    assert len(alerter.handle_event(_envelope(camera="cam-2"))) == 1
    assert dispatcher.fire.call_count == 2


def test_dedup_zero_fires_every_read():
    alerter, _ = _alerter(dedup_window_seconds=0.0)
    assert len(alerter.handle_event(_envelope())) == 1
    assert len(alerter.handle_event(_envelope())) == 1


def test_dedup_expires_after_window(monkeypatch):
    alerter, _ = _alerter(dedup_window_seconds=10.0)
    t = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["now"])
    assert len(alerter.handle_event(_envelope())) == 1
    t["now"] += 11.0
    assert len(alerter.handle_event(_envelope())) == 1


# ── Confidence + malformed input ────────────────────────────────────


def test_min_confidence_drops_weak_reads():
    alerter, _ = _alerter(min_confidence=0.5)
    assert alerter.handle_event(_envelope(confidence=0.3)) == []
    assert len(alerter.handle_event(_envelope(confidence=0.9))) == 1
    # An event WITHOUT confidence must not be dropped by the filter.
    alerter2, _ = _alerter(min_confidence=0.5, dedup_window_seconds=0)
    assert len(alerter2.handle_event(_envelope(confidence=None))) == 1


def test_foreign_and_malformed_events_are_ignored():
    alerter, dispatcher = _alerter()
    assert alerter.handle_event("junk") == []
    assert alerter.handle_event({"schema": "opennvr.tier0.v1"}) == []
    assert alerter.handle_event(_envelope(schema="plate.recognized.v2")) == []
    assert alerter.handle_event(_envelope(payload="junk")) == []
    assert alerter.handle_event(_envelope(plate="")) == []
    assert alerter.handle_event(_envelope(camera="")) == []
    assert dispatcher.fire.call_count == 0


# ── Camera scope (Phase 2 integration) ──────────────────────────────


def test_explicit_camera_scope_wins():
    alerter, _ = _alerter(cameras=["cam-1"])
    assert len(alerter.handle_event(_envelope(camera="cam-1"))) == 1
    assert alerter.handle_event(_envelope(camera="cam-9")) == []


def test_assignment_scope_via_sdk(monkeypatch):
    alerter, _ = _alerter(opennvr_url="http://core:8000")
    monkeypatch.setattr(lpr, "cameras_for_skill",
                        lambda url, skill, api_key=None: ["cam-7"])
    assert alerter.handle_event(_envelope(camera="cam-1")) == []
    assert len(alerter.handle_event(_envelope(camera="cam-7"))) == 1


def test_scope_fetch_failure_means_no_restriction(monkeypatch):
    alerter, _ = _alerter(opennvr_url="http://core:8000")

    def boom(url, skill, api_key=None):
        raise RuntimeError("core down")
    monkeypatch.setattr(lpr, "cameras_for_skill", boom)
    assert len(alerter.handle_event(_envelope(camera="anything"))) == 1


# ── Contract surface ────────────────────────────────────────────────


def test_state_snapshot_shape():
    alerter, _ = _alerter(allowlist=["OK1"], denylist=["BAD1", "BAD2"])
    alerter.handle_event(_envelope())
    snap = alerter.state_snapshot()
    assert snap["allowlist_size"] == 1
    assert snap["denylist_size"] == 2
    assert snap["deduped_plates_tracked"] == 1
    assert snap["recent"][0]["message"] == "ABC1234 on cam-1"


def test_live_watchlist_update_applies_atomically():
    alerter, _ = _alerter(dedup_window_seconds=0)
    assert alerter.handle_event(_envelope(plate="XYZ9"))[0].severity == "info"
    alerter.on_config_update({"denylist": ["xyz9 "], "allowlist": []})
    assert alerter.handle_event(_envelope(plate="XYZ9"))[0].severity == "high"


def test_manifest_declares_the_consumer_contract():
    m = PlateAlerter.manifest
    assert m.subscribes == PLATE_SUBJECT_PATTERN
    assert m.version == "2.0.0"
    assert "object_detection" in m.requires_tasks
    assert "license_plate_recognition" in m.requires_tasks


# ── Config loader ───────────────────────────────────────────────────


def test_load_config_requires_nats_url(tmp_path: Path):
    p = tmp_path / "c.yml"
    p.write_text("dedup_window_seconds: 5\n")
    with pytest.raises(ValueError, match="nats_url"):
        load_config(p)


def test_load_config_normalises(tmp_path: Path):
    p = tmp_path / "c.yml"
    p.write_text(
        "nats_url: nats://n:4222\n"
        "allowlist: [' ab12 ', '']\n"
        "denylist: ['bad1']\n"
        "cameras: ['cam-1', ' ']\n"
    )
    cfg = load_config(p)
    assert cfg.subject_pattern == PLATE_SUBJECT_PATTERN
    assert cfg.allowlist == ["AB12", ""]  # blank filtered at setup, not load
    assert cfg.denylist == ["BAD1"]
    assert cfg.cameras == ["cam-1"]
