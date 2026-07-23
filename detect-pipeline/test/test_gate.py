# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Tier-1 gate — every decision path, no I/O."""
from __future__ import annotations

from detect_pipeline.gate import Gate, GateConfig, TriggerPolicy
from detect_pipeline.tracking import Track


def mk(tid=1, label="person", hits=2, motionless=0, box=(10, 10, 30, 40), score=0.9):
    return Track(id=tid, label=label, box=box, score=score, hits=hits,
                 motionless_count=motionless, stationary_threshold=50)


def reasons(res):
    return {(d.track_id, d.reason, d.escalate) for d in res.decisions}


# ─────────────────── default = shadow (measure-only) ───────────────────

def test_shadow_is_default_and_dispatches_nothing():
    g = Gate()  # shadow=True by default
    res = g.evaluate([mk()], now=0.0)
    assert res.shadow is True and res.enforced is False
    assert res.decisions[0].escalate is True           # decision computed…
    assert res.decisions[0].reason == "new_track"
    assert res.to_dispatch() == []                     # …but nothing is enforced


def test_enforced_dispatch_when_not_shadow():
    g = Gate(GateConfig(shadow=False))
    res = g.evaluate([mk()], now=0.0)
    assert res.enforced is True
    assert [d.track_id for d in res.to_dispatch()] == [1]


# ─────────────────────── core escalate/suppress ───────────────────────

def test_new_track_escalates_then_cooldown_then_refresh():
    g = Gate(GateConfig(shadow=False, escalate_cooldown_s=30))
    assert g.evaluate([mk()], now=0.0).decisions[0].reason == "new_track"
    assert g.evaluate([mk()], now=5.0).decisions[0].reason == "cooldown"   # suppressed
    d = g.evaluate([mk()], now=40.0).decisions[0]
    assert d.escalate and d.reason == "refresh"        # past cooldown, re-look


def test_stationary_is_suppressed():
    g = Gate(GateConfig(shadow=False))
    d = g.evaluate([mk(motionless=60)], now=0.0).decisions[0]   # >= threshold 50
    assert d.escalate is False and d.reason == "stationary"


def test_reactivated_stationary_object_escalates():
    g = Gate(GateConfig(shadow=False))
    g.evaluate([mk(motionless=60)], now=0.0)                    # settled -> suppressed
    d = g.evaluate([mk(motionless=0)], now=1.0).decisions[0]    # moves again
    assert d.escalate and d.reason == "reactivated"


def test_not_confirmed_is_suppressed():
    g = Gate(GateConfig(shadow=False, min_hits=3))
    d = g.evaluate([mk(hits=1)], now=0.0).decisions[0]
    assert d.escalate is False and d.reason == "not_confirmed"


def test_uninteresting_class_suppressed():
    g = Gate(GateConfig(shadow=False, interesting_classes=frozenset({"person"})))
    d = g.evaluate([mk(label="cat")], now=0.0).decisions[0]
    assert d.escalate is False and d.reason == "uninteresting_class"


# ─────────────────────────── safety rails ───────────────────────────

def test_critical_class_escalates_even_when_stationary():
    g = Gate(GateConfig(shadow=False, critical_classes=frozenset({"person"})))
    d = g.evaluate([mk(motionless=60)], now=0.0).decisions[0]   # stationary…
    assert d.escalate and d.reason == "critical_class"          # …but forced


def test_always_analyze_bypasses_suppression():
    g = Gate(GateConfig(shadow=False, always_analyze=True,
                        interesting_classes=frozenset({"person"})))
    d = g.evaluate([mk(label="cat", motionless=60)], now=0.0).decisions[0]
    assert d.escalate and d.reason == "always_analyze"


def test_zone_escalates():
    g = Gate(GateConfig(shadow=False, zones=((0, 0, 100, 100),)))
    d = g.evaluate([mk(motionless=60)], now=0.0).decisions[0]
    assert d.escalate and d.reason == "in_zone"


def test_zone_miss_does_not_force():
    g = Gate(GateConfig(shadow=False, zones=((200, 200, 300, 300),)))
    d = g.evaluate([mk(box=(10, 10, 30, 40))], now=0.0).decisions[0]
    assert d.reason == "new_track"        # not in the zone -> normal path


def test_heartbeat_forces_pass_on_static_scene():
    g = Gate(GateConfig(shadow=False, heartbeat_s=10))
    assert g.evaluate([mk(motionless=60)], now=0.0).decisions[0].reason == "stationary"
    d = g.evaluate([mk(motionless=60)], now=12.0).decisions[0]
    assert d.escalate and d.reason == "heartbeat"


def test_always_analyze_ignores_cooldown():
    # always_analyze disables the gate for the camera -> escalate every frame,
    # NOT once per cooldown (that would silently re-enable gating).
    g = Gate(GateConfig(shadow=False, always_analyze=True, escalate_cooldown_s=30))
    assert g.evaluate([mk()], now=0.0).decisions[0].reason == "always_analyze"
    d = g.evaluate([mk()], now=5.0).decisions[0]      # well within cooldown
    assert d.escalate and d.reason == "always_analyze"


def test_heartbeat_does_not_fire_on_first_frame_with_real_clock():
    # In production `now` is frame.ts (a large monotonic/epoch value). The first
    # frame must NOT spuriously heartbeat — the clock anchors to it instead.
    g = Gate(GateConfig(shadow=False, heartbeat_s=10))
    assert g.evaluate([mk(tid=1)], now=1000.0).decisions[0].reason == "new_track"
    # …and it still fires once heartbeat_s has genuinely elapsed since the anchor.
    d = g.evaluate([mk(tid=1, motionless=60)], now=1011.0).decisions[0]
    assert d.escalate and d.reason == "heartbeat"


def test_heartbeat_fires_even_within_cooldown():
    # heartbeat is a hard latency floor: it must fire even if the track is still
    # in its per-track cooldown (heartbeat_s < cooldown_s).
    g = Gate(GateConfig(shadow=False, heartbeat_s=10, escalate_cooldown_s=30))
    assert g.evaluate([mk(tid=1)], now=0.0).decisions[0].reason == "new_track"
    assert g.evaluate([mk(tid=1)], now=5.0).decisions[0].reason == "cooldown"
    d = g.evaluate([mk(tid=1)], now=12.0).decisions[0]   # still <30s cooldown
    assert d.escalate and d.reason == "heartbeat"


# ───────────────────────────── lifecycle ─────────────────────────────

def test_reused_id_after_disappearance_looks_new_again():
    g = Gate(GateConfig(shadow=False))
    assert g.evaluate([mk(tid=7)], now=0.0).decisions[0].reason == "new_track"
    g.evaluate([], now=1.0)                              # track 7 disappears
    d = g.evaluate([mk(tid=7)], now=2.0).decisions[0]    # id reused later
    assert d.escalate and d.reason == "new_track"


def test_trigger_policy_default_is_motion():
    assert GateConfig().trigger is TriggerPolicy.MOTION


def test_multiple_tracks_decided_independently():
    g = Gate(GateConfig(shadow=False))
    res = g.evaluate([mk(tid=1, label="person"), mk(tid=2, label="cat", motionless=60)], now=0.0)
    got = reasons(res)
    assert (1, "new_track", True) in got
    assert (2, "stationary", False) in got


# ─────────────────────── gate-decision audit ───────────────────────

class _Frame:
    seq, ts = 42, 1837.5


def test_gate_audit_payload_records_escalations_and_suppressions():
    from detect_pipeline.bus import build_gate_payload, gate_subject_for
    g = Gate(GateConfig(shadow=False))
    res = g.evaluate([mk(tid=1, label="person"), mk(tid=2, label="cat", motionless=60)], now=0.0)
    p = build_gate_payload("cam_3", res, _Frame())
    assert gate_subject_for("cam_3") == "opennvr.inference.tier0.cam_3.gate"
    assert p["schema"] == "opennvr.tier0.gate.v1" and p["shadow"] is False
    assert p["escalated"] == 1 and p["suppressed"] == 1        # both sides audited
    reasons_by_id = {d["id"]: d["reason"] for d in p["decisions"]}
    assert reasons_by_id == {1: "new_track", 2: "stationary"}


def test_gate_event_sink_publishes_incl_non_events():
    import json

    from detect_pipeline.bus import GateEventSink
    sent = []
    sink = GateEventSink(lambda subj, data: sent.append((subj, json.loads(data))))
    g = Gate()  # shadow default
    res = g.evaluate([mk(motionless=60)], now=0.0)   # a pure suppression
    sink.publish("cam_1", res, _Frame())
    assert len(sent) == 1                            # non-event still audited
    subj, payload = sent[0]
    assert subj == "opennvr.inference.tier0.cam_1.gate"
    assert payload["suppressed"] == 1 and payload["shadow"] is True


def test_gate_event_sink_skips_empty_by_default():
    from detect_pipeline.bus import GateEventSink
    sent = []
    GateEventSink(lambda s, d: sent.append(s)).publish(
        "cam_1", Gate().evaluate([], now=0.0), _Frame())
    assert sent == []
