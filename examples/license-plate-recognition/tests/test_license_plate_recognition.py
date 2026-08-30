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
    assert m.version == "2.4.0"
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


# ── The /ui dashboard (RFC-0002 Phase 4 app-surface convention) ────


def test_manifest_declares_the_ui():
    assert PlateAlerter.manifest.has_ui is True


def test_ui_html_renders_state_and_escapes():
    alerter, _ = _alerter(allowlist=["OK1"], denylist=["BAD1"])
    # A hostile "plate" from a rogue producer must render inert — the
    # page is sandboxed AND escaped (defense in depth).
    alerter.handle_event(_envelope(plate="<script>x</script>AB", camera="cam-1"))
    html = alerter.ui_html()
    assert "License Plate Recognition" in html
    # The plate is normalised (upper, no separators) before storage, so
    # the hostile payload survives as <SCRIPT>… — assert the ESCAPED
    # uppercase form renders and the raw tag never does.
    assert "<SCRIPT>" not in html and "<script>" not in html
    assert "&lt;SCRIPT&gt;" in html
    assert ">1<" in html                         # allowlist count renders
    assert "App Catalog" in html                 # points at the config form
    assert "<script" not in html.lower()         # the page itself has no JS


# ── The society register + unknown-vehicle alarms ───────────────────


def test_registry_plate_fires_low_with_owner_in_title():
    alerter, _ = _alerter(registry=[
        {"plate": "mh 12 de 1433", "owner": "A. Sharma", "unit": "B-402"},
    ])
    fired = alerter.handle_event(_envelope(plate="MH12DE1433"))
    assert fired[0].severity == "low"
    assert "Registered vehicle MH12DE1433" in fired[0].title
    assert "A. Sharma" in fired[0].title and "B-402" in fired[0].title
    assert fired[0].evidence["in_registry"] is True
    assert fired[0].evidence["registry"]["owner"] == "A. Sharma"
    assert fired[0].evidence["unknown_alarm"] is False


def test_unknown_alarm_fires_high_for_unregistered_plate():
    alerter, _ = _alerter(alarm_on_unknown=True,
                          registry=["MH12DE1433"])
    stranger = alerter.handle_event(_envelope(plate="XX99ZZ0001"))
    assert stranger[0].severity == "high"
    assert "Unknown vehicle XX99ZZ0001" in stranger[0].title
    assert stranger[0].evidence["unknown_alarm"] is True
    # The registered vehicle stays quiet (low, not an alarm).
    known = alerter.handle_event(_envelope(plate="MH12DE1433"))
    assert known[0].severity == "low"
    assert known[0].evidence["unknown_alarm"] is False


def test_unknown_alarm_off_by_default_keeps_info_reads():
    alerter, _ = _alerter(registry=["MH12DE1433"])
    fired = alerter.handle_event(_envelope(plate="XX99ZZ0001"))
    assert fired[0].severity == "info"
    assert fired[0].evidence["unknown_alarm"] is False


def test_unknown_cooldown_is_per_plate_across_cameras(monkeypatch):
    alerter, _ = _alerter(alarm_on_unknown=True,
                          unknown_cooldown_seconds=300.0,
                          dedup_window_seconds=0)
    t = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["now"])
    first = alerter.handle_event(_envelope(plate="XX99ZZ0001", camera="cam-1"))
    assert first[0].severity == "high"
    # Same stranger at the SECOND gate camera 30s later: still a read,
    # but not a second alarm — one stranger, one alarm.
    t["now"] += 30.0
    second = alerter.handle_event(_envelope(plate="XX99ZZ0001", camera="cam-2"))
    assert second[0].severity == "info"
    assert second[0].evidence["unknown_alarm"] is False
    # After the cooldown the alarm can fire again.
    t["now"] += 300.0
    third = alerter.handle_event(_envelope(plate="XX99ZZ0001", camera="cam-1"))
    assert third[0].severity == "high"
    assert alerter.state_snapshot()["unknown_alarms"] == 2


def test_denylist_beats_registry():
    alerter, _ = _alerter(alarm_on_unknown=True,
                          denylist=["BAD001"],
                          registry=["BAD001"])
    fired = alerter.handle_event(_envelope(plate="BAD001"))
    assert fired[0].severity == "high"
    assert "Monitored plate" in fired[0].title
    assert fired[0].evidence["unknown_alarm"] is False


def test_parse_registry_accepts_strings_dicts_and_skips_bad_rows():
    reg = lpr.parse_registry([
        "ka 05 mj 6021",
        {"plate": "MH14GH0007", "owner": "R. Iyer", "unit": "A-101",
         "type": "car", "junk": "dropped"},
        {"owner": "no plate — skipped"},
        42,
        "",
    ])
    assert set(reg) == {"KA05MJ6021", "MH14GH0007"}
    assert reg["KA05MJ6021"] == {}
    assert reg["MH14GH0007"] == {"owner": "R. Iyer", "unit": "A-101", "type": "car"}
    assert "junk" not in reg["MH14GH0007"]


def test_live_registry_and_alarm_update():
    alerter, _ = _alerter(dedup_window_seconds=0)
    # Initially: unknown mode off, stranger is an info read.
    assert alerter.handle_event(_envelope(plate="XX99ZZ0001"))[0].severity == "info"
    alerter.on_config_update({
        "allowlist": [], "denylist": [],
        "registry": [{"plate": "XX99ZZ0001", "owner": "New resident"}],
        "alarm_on_unknown": True,
    })
    # Now registered → low; a different stranger → high alarm.
    assert alerter.handle_event(_envelope(plate="XX99ZZ0001"))[0].severity == "low"
    assert alerter.handle_event(_envelope(plate="YY88AA0002"))[0].severity == "high"
    snap = alerter.state_snapshot()
    assert snap["registry_size"] == 1
    assert snap["alarm_on_unknown"] is True


def test_state_snapshot_reports_register():
    alerter, _ = _alerter(registry=["A1", "B2"], alarm_on_unknown=True)
    snap = alerter.state_snapshot()
    assert snap["registry_size"] == 2
    assert snap["alarm_on_unknown"] is True
    assert snap["unknown_alarms"] == 0


def test_denylist_plate_never_counts_as_unknown():
    # In alarm mode a watchlisted plate NOT in the registry must fire as
    # a WATCHLIST alarm — and must not touch the unknown-cooldown ledger
    # or counter (it is a known-bad vehicle, not a stranger).
    alerter, _ = _alerter(alarm_on_unknown=True, denylist=["BAD001"])
    fired = alerter.handle_event(_envelope(plate="BAD001"))
    assert fired[0].severity == "high"
    assert "Monitored plate" in fired[0].title
    assert fired[0].evidence["unknown_alarm"] is False
    assert alerter.state_snapshot()["unknown_alarms"] == 0


# ── Vehicle model + visitor-pass expiry ─────────────────────────────


def test_parse_registry_keeps_model_and_expires():
    reg = lpr.parse_registry([
        {"plate": "MH12DE1433", "owner": "A. Sharma", "model": "Honda City",
         "expires": "2030-01-31"},
    ])
    assert reg["MH12DE1433"]["model"] == "Honda City"
    assert reg["MH12DE1433"]["expires"] == "2030-01-31"


def test_expired_pass_counts_as_unknown():
    from datetime import date as _date
    assert lpr.registry_entry_active({"expires": "2030-01-01"},
                                     today=_date(2026, 8, 30)) is True
    assert lpr.registry_entry_active({"expires": "2026-08-29"},
                                     today=_date(2026, 8, 30)) is False
    # Boundary: the expiry DAY itself is still valid.
    assert lpr.registry_entry_active({"expires": "2026-08-30"},
                                     today=_date(2026, 8, 30)) is True
    # A typo'd date must NOT turn a resident into a stranger.
    assert lpr.registry_entry_active({"expires": "not-a-date"}) is True
    assert lpr.registry_entry_active({}) is True
    assert lpr.registry_entry_active(None) is False


def test_expired_visitor_pass_alarms_when_unknown_mode_on():
    alerter, _ = _alerter(alarm_on_unknown=True, registry=[
        {"plate": "GU3STPASS1", "owner": "Visitor", "expires": "2020-01-01"},
        {"plate": "MH12DE1433", "owner": "Resident"},
    ])
    expired = alerter.handle_event(_envelope(plate="GU3STPASS1"))
    assert expired[0].severity == "high"
    assert "Unknown vehicle" in expired[0].title
    assert expired[0].evidence["registry_expired"] is True
    assert expired[0].evidence["in_registry"] is False
    resident = alerter.handle_event(_envelope(plate="MH12DE1433"))
    assert resident[0].severity == "low"
    assert resident[0].evidence["registry_expired"] is False


# ── Per-plate monitors (configurable surveillance alerts) ───────────


def test_monitor_fires_with_configured_severity_and_note():
    alerter, _ = _alerter(monitors=[
        {"plate": "mh 12 de 1433", "note": "court order 42/2026",
         "severity": "critical"},
    ])
    fired = alerter.handle_event(_envelope(plate="MH12DE1433"))
    assert fired[0].severity == "critical"
    assert "Monitored plate MH12DE1433 seen — court order 42/2026" in fired[0].title
    assert fired[0].evidence["monitor"] == {
        "severity": "critical", "note": "court order 42/2026"}


def test_inactive_monitor_is_silent_but_still_known():
    alerter, _ = _alerter(alarm_on_unknown=True, monitors=[
        {"plate": "BAD001", "active": False},
    ])
    fired = alerter.handle_event(_envelope(plate="BAD001"))
    # Not the monitor alert…
    assert fired[0].evidence["monitor"] is None
    # …and NOT an unknown-vehicle alarm either: the plate is known.
    assert fired[0].evidence["unknown_alarm"] is False
    assert fired[0].severity == "info"


def test_monitor_camera_scope_restricts_where_it_fires():
    alerter, _ = _alerter(dedup_window_seconds=0, monitors=[
        {"plate": "BAD001", "cameras": ["cam-2"], "severity": "high"},
    ])
    at_gate = alerter.handle_event(_envelope(plate="BAD001", camera="cam-1"))
    assert at_gate[0].evidence["monitor"] is None
    assert at_gate[0].severity == "info"
    at_scope = alerter.handle_event(_envelope(plate="BAD001", camera="cam-2"))
    assert at_scope[0].evidence["monitor"] is not None
    assert at_scope[0].severity == "high"


def test_explicit_monitor_wins_over_denylist_shorthand():
    alerter, _ = _alerter(denylist=["BAD001"], monitors=[
        {"plate": "BAD001", "severity": "medium", "note": "downgraded"},
    ])
    fired = alerter.handle_event(_envelope(plate="BAD001"))
    assert fired[0].severity == "medium"


def test_bad_monitor_severity_falls_back_to_high():
    reg = lpr.parse_monitors([{"plate": "X1", "severity": "apocalyptic"}])
    assert reg["X1"]["severity"] == "high"


def test_monitors_update_live():
    alerter, _ = _alerter(dedup_window_seconds=0)
    assert alerter.handle_event(_envelope(plate="NEW999"))[0].severity == "info"
    alerter.on_config_update({
        "allowlist": [], "denylist": [],
        "monitors": [{"plate": "NEW999", "severity": "high", "note": "stolen"}],
    })
    fired = alerter.handle_event(_envelope(plate="NEW999"))
    assert fired[0].severity == "high"
    assert "stolen" in fired[0].title
    assert alerter.state_snapshot()["monitored_plates"] == 1


def test_manifest_declares_every_key_the_vehicles_page_writes():
    """Core's PUT /config validator REJECTS unknown config keys, so
    every key the Vehicles page can write must be a declared param —
    this list is the frontend's write-surface, kept in lockstep."""
    declared = {p.name for p in PlateAlerter.manifest.params}
    page_writes = {
        "allowlist", "denylist", "monitors", "registry",
        "alarm_on_unknown", "unknown_cooldown_seconds", "camera_roles",
        "barrier_mode",
    }
    missing = page_writes - declared
    assert not missing, f"undeclared config keys the page writes: {missing}"


# ── Gate automation (access.decided.v1) ─────────────────────────────


def _decisions(alerter):
    """Patch the publisher; return the captured publish calls."""
    calls = []

    def fake_publish(schema, *, camera_id, payload, correlation_id=None):
        calls.append({"schema": schema, "camera_id": camera_id,
                      "payload": payload, "correlation_id": correlation_id})
        return True

    alerter._decision_publisher.publish = fake_publish
    return calls


def test_barrier_off_publishes_nothing():
    alerter, _ = _alerter(camera_roles={"1": {"role": "gate_in"}},
                          registry=["MH12DE1433"])
    calls = _decisions(alerter)
    alerter.handle_event(_envelope(plate="MH12DE1433", camera="cam1"))
    assert calls == []


def test_registered_plate_on_gate_in_allows():
    alerter, _ = _alerter(
        barrier_mode="registered",
        camera_roles={"1": {"role": "gate_in"}, "2": {"role": "gate_out"}},
        registry=[{"plate": "MH12DE1433", "owner": "A. Sharma", "unit": "B-402"}])
    calls = _decisions(alerter)
    alerter.handle_event(_envelope(plate="MH12DE1433", camera="cam1"))
    assert len(calls) == 1
    d = calls[0]
    assert d["schema"] == "access.decided.v1"
    assert d["camera_id"] == "cam1"
    assert d["payload"]["decision"] == "allow"
    assert d["payload"]["reason"] == "registered"
    assert d["payload"]["owner"] == "A. Sharma"
    assert d["correlation_id"] == "corr-1"
    assert alerter.state_snapshot()["decisions_published"] == 1


def test_unknown_plate_on_gate_in_denies():
    alerter, _ = _alerter(barrier_mode="registered",
                          camera_roles={"1": {"role": "gate_in"}})
    calls = _decisions(alerter)
    alerter.handle_event(_envelope(plate="XX99ZZ0001", camera="cam1"))
    assert calls[0]["payload"] == {
        "plate_text": "XX99ZZ0001", "decision": "deny", "reason": "unknown",
        "owner": None, "unit": None, "confidence": 0.9}


def test_monitored_plate_denied_even_if_registered():
    alerter, _ = _alerter(
        barrier_mode="registered",
        camera_roles={"1": {"role": "gate_in"}},
        registry=["BAD001"], denylist=["BAD001"])
    calls = _decisions(alerter)
    alerter.handle_event(_envelope(plate="BAD001", camera="cam1"))
    assert calls[0]["payload"]["decision"] == "deny"
    assert calls[0]["payload"]["reason"] == "monitored"


def test_expired_pass_denied_with_reason():
    alerter, _ = _alerter(
        barrier_mode="registered",
        camera_roles={"1": {"role": "gate_in"}},
        registry=[{"plate": "GU3ST1", "owner": "Guest",
                   "expires": "2020-01-01"}])
    calls = _decisions(alerter)
    alerter.handle_event(_envelope(plate="GU3ST1", camera="cam1"))
    assert calls[0]["payload"]["decision"] == "deny"
    assert calls[0]["payload"]["reason"] == "expired_pass"
    assert calls[0]["payload"]["owner"] == "Guest"


def test_non_gate_camera_publishes_no_decision():
    alerter, _ = _alerter(barrier_mode="registered",
                          camera_roles={"1": {"role": "gate_in"}},
                          registry=["MH12DE1433"])
    calls = _decisions(alerter)
    alerter.handle_event(_envelope(plate="MH12DE1433", camera="cam2"))
    assert calls == []


def test_gate_in_cameras_accepts_ids_and_handles():
    got = lpr.gate_in_cameras({
        "1": {"role": "gate_in"}, "cam7": {"role": "gate_in"},
        "2": {"role": "gate_out"}, "3": {"role": "parking"},
        "x": "gate_in",
    })
    assert got == frozenset({"cam1", "cam7", "camx"})


def test_barrier_config_updates_live():
    alerter, _ = _alerter(registry=["MH12DE1433"])
    calls = _decisions(alerter)
    alerter.handle_event(_envelope(plate="MH12DE1433", camera="cam1"))
    assert calls == []  # off by default
    alerter.on_config_update({
        "allowlist": [], "denylist": [],
        "barrier_mode": "registered",
        "camera_roles": {"1": {"role": "gate_in"}},
    })
    alerter.handle_event(_envelope(plate="MH12DE1433", camera="cam1",
                                   confidence=0.8))
    # dedup: same (camera, plate) within window suppresses — use a
    # fresh plate to prove the live switch took effect.
    alerter.handle_event(_envelope(plate="KA05MJ6021", camera="cam1"))
    assert len(calls) >= 1
    assert calls[-1]["payload"]["decision"] in ("allow", "deny")
