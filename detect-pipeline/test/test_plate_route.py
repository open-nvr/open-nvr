# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RFC-0002 Phase 4: the plate chain as a per-camera declarative route.

Pinned behaviours:

* the plate route exists ONLY on cameras assigned the
  license_plate_recognition skill — an unassigned camera's router is
  the shared base, untouched (plates are opt-in per camera, decision 9
  + the privacy default);
* fast_plate_ocr dispatches at most once per (camera, track): one OCR
  per vehicle visit, while caption keeps its cooldown pacing;
* the infer body's task is per-adapter (fast_plate_ocr serves
  license_plate_recognition, not the dispatcher-wide "caption");
* the provider threads a camera's assigned skills into its spec, so an
  assignment change restarts the worker like a label change does.
"""
from __future__ import annotations

from detect_pipeline.dispatch import (
    ADAPTER_TASKS,
    DEFAULT_ROUTES,
    DispatchRouter,
    OncePerTrack,
    dispatch_escalations,
    router_for_skills,
)
from detect_pipeline.gate import GateDecision, GateResult
from detect_pipeline.providers import _assignment_view


class _Track:
    def __init__(self, tid, label="car"):
        self.id = tid
        self.label = label
        self.best_crop = object()


class _FakeDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, cam, adapter, crop, track):
        self.calls.append((cam, adapter, track.id))


def _escalate(*tracks):
    return GateResult(decisions=[
        GateDecision(t.id, t.label, True, "escalate", False) for t in tracks
    ], shadow=False)


# ── router_for_skills ──────────────────────────────────────────────


def test_assigned_camera_gains_the_plate_route():
    base = DispatchRouter()
    r = router_for_skills(frozenset({"license_plate_recognition"}), base=base)
    assert r is not base
    assert r.route("car") == ["caption", "fast_plate_ocr"]
    assert r.route("truck") == ["caption", "fast_plate_ocr"]
    assert r.route("person") == ["caption"]        # people never route to OCR
    # The shared base router must be untouched (other cameras use it).
    assert base.route("car") == ["caption"]
    assert DEFAULT_ROUTES["car"] == ["caption"]


def test_unassigned_camera_shares_the_base_router_identity():
    base = DispatchRouter()
    assert router_for_skills(None, base=base) is base
    assert router_for_skills(frozenset({"object_detection"}), base=base) is base
    assert router_for_skills(None, base=None) is None


# ── once-per-track (one OCR per visit) ─────────────────────────────


def test_plate_dispatch_fires_once_per_track():
    fd = _FakeDispatcher()
    router = router_for_skills(frozenset({"license_plate_recognition"}),
                               base=DispatchRouter())
    once = OncePerTrack()
    car = _Track(7, "car")
    # First escalation: caption + OCR both fire.
    dispatch_escalations("cam1", [car], _escalate(car), router, fd, once=once)
    # Cooldown re-escalation of the SAME track: caption again, OCR not.
    dispatch_escalations("cam1", [car], _escalate(car), router, fd, once=once)
    adapters = [a for (_, a, _) in fd.calls]
    assert adapters.count("fast_plate_ocr") == 1
    assert adapters.count("caption") == 2
    # A NEW track (new visit) gets its own OCR.
    car2 = _Track(8, "car")
    dispatch_escalations("cam1", [car2], _escalate(car2), router, fd, once=once)
    assert [a for (_, a, t) in fd.calls if t == 8].count("fast_plate_ocr") == 1


def test_no_once_filter_keeps_legacy_behaviour():
    fd = _FakeDispatcher()
    router = router_for_skills(frozenset({"license_plate_recognition"}),
                               base=DispatchRouter())
    car = _Track(7, "car")
    dispatch_escalations("cam1", [car], _escalate(car), router, fd)
    dispatch_escalations("cam1", [car], _escalate(car), router, fd)
    assert [a for (_, a, _) in fd.calls].count("fast_plate_ocr") == 2


def test_once_per_track_is_bounded():
    once = OncePerTrack(maxlen=2)
    for tid in (1, 2, 3):                  # 3 evicts (1, x)
        assert once.seen(tid, "x") is False
        once.mark(tid, "x")
    assert once.seen(1, "x") is False      # aged out -> re-dispatch once
    once.mark(1, "x")
    assert once.seen(1, "x") is True


def test_backpressure_drop_does_not_consume_the_visits_ocr():
    # A dropped dispatch (False) must NOT mark the track: the next
    # escalation retries the visit's one OCR instead of losing it.
    router = router_for_skills(frozenset({"license_plate_recognition"}),
                               base=DispatchRouter())
    once = OncePerTrack()
    car = _Track(7, "car")

    class _Dropping:
        def __init__(self):
            self.calls = []
        def dispatch(self, cam, adapter, crop, track):
            self.calls.append(adapter)
            return False                   # explicit drop
    dropping = _Dropping()
    dispatch_escalations("cam1", [car], _escalate(car), router, dropping,
                         once=once)
    assert "fast_plate_ocr" in dropping.calls
    # Retry after the drop: the OCR fires again (and marks this time).
    accepting = _FakeDispatcher()
    dispatch_escalations("cam1", [car], _escalate(car), router, accepting,
                         once=once)
    assert [a for (_, a, _) in accepting.calls].count("fast_plate_ocr") == 1
    # Now marked: a third escalation does not re-OCR.
    dispatch_escalations("cam1", [car], _escalate(car), router, accepting,
                         once=once)
    assert [a for (_, a, _) in accepting.calls].count("fast_plate_ocr") == 1


# ── per-adapter task ───────────────────────────────────────────────


def test_fast_plate_ocr_task_is_license_plate_recognition():
    assert ADAPTER_TASKS["fast_plate_ocr"] == "license_plate_recognition"


# ── provider threads skills into the spec ──────────────────────────


def test_assignment_view_returns_skills():
    labels, analyze, skills = _assignment_view({
        "camera_id": "cam1",
        "assignments": [
            {"skill": "license_plate_recognition"},
            {"skill": "object_detection", "labels": ["Person"]},
        ],
    })
    assert skills == frozenset({"license_plate_recognition",
                                "object_detection"})
    assert labels == frozenset({"person"})
    assert analyze is True


def test_assignment_view_no_assignments_means_no_skills():
    assert _assignment_view({"camera_id": "c"}) == (None, True, None)
    assert _assignment_view({"camera_id": "c", "assignments": []}) == (
        None, True, None)
