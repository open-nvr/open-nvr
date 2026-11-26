# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The face route (RFC-0002 decision 9: ``person -> face adapter``) as an
opt-in, per-camera route.

Pinned behaviours:

* the route exists only on a camera whose claims include
  face_recognition AND name an adapter (each face app has its own
  gallery); other cameras keep the shared base router;
* it runs whatever the gate mode: in shadow the base router is empty, so
  a face camera sends people to its face adapter and nothing else runs
  anywhere; in enforce the default routes run beside it, and a face
  adapter is never sent twice for one escalation;
* a person is re-sent while their best crop improves, at most every
  min interval, until a result recognises them or the attempt cap;
* the provider reads the adapters from the claims into the spec, and
  dispatch is built the same way at start and on a gate-mode change.
"""
from __future__ import annotations

from types import SimpleNamespace

from detect_pipeline.dispatch import (
    FACE_TASK,
    DispatchRouter,
    FaceFollowUp,
    dispatch_escalations,
    face_recognised,
    follow_up_faces,
    router_for_skills,
)
from detect_pipeline.gate import GateDecision, GateResult
from detect_pipeline.providers import _skill_adapters, _to_spec

FACE = "insightface_school"
FACE_CLAIM = frozenset({("face_recognition", FACE)})


class _Track:
    def __init__(self, tid, label="person", crop_ts=1.0):
        self.id = tid
        self.label = label
        self.best_crop = object()
        self.best_crop_ts = crop_ts


class _FakeDispatcher:
    def __init__(self, accept=True):
        self.calls = []
        self.accept = accept

    def dispatch(self, cam, adapter, crop, track, **kw):
        self.calls.append((cam, adapter, track.id, kw.get("task")))
        self.last_on_result = kw.get("on_result")
        return self.accept


def _gate(*tracks, shadow, escalate=True):
    return GateResult(decisions=[
        GateDecision(t.id, t.label, escalate, "new_track", shadow) for t in tracks
    ], shadow=shadow)


# -- routing ----------------------------------------------------------------

def test_face_claim_routes_people_to_the_claiming_adapter():
    base = DispatchRouter()
    r = router_for_skills(frozenset({"face_recognition"}), base=base, skill_adapters=FACE_CLAIM)
    assert r is not base
    assert r.route("person") == ["caption", FACE]
    assert r.face_adapters == frozenset({FACE})
    assert r.route("car") == ["caption"]            # vehicles never go to faces
    assert base.route("person") == ["caption"] and not base.face_adapters


def test_face_claim_without_an_adapter_adds_nothing():
    base = DispatchRouter()
    assert router_for_skills(frozenset({"face_recognition"}), base=base) is base
    assert router_for_skills(frozenset({"object_detection"}), base=base,
                             skill_adapters=FACE_CLAIM) is base


def test_plates_and_faces_together():
    r = router_for_skills(frozenset({"face_recognition", "license_plate_recognition"}),
                          base=DispatchRouter(), skill_adapters=FACE_CLAIM)
    assert r.route("person") == ["caption", FACE]
    assert r.route("car") == ["caption", "fast_plate_ocr"]


# -- dispatch in each gate mode --------------------------------------------

def test_shadow_gate_sends_people_to_the_face_adapter_only():
    # shadow: the base router is empty (run._build_dispatch), so the face
    # route is the only thing that can run.
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(routes={}),
                          skill_adapters=FACE_CLAIM)
    t = _Track(1)
    fd = _FakeDispatcher()
    n = dispatch_escalations("cam1", [t], _gate(t, shadow=True), r, fd, follow=FaceFollowUp(), now=10.0)
    assert n == 1
    assert fd.calls == [("cam1", FACE, 1, FACE_TASK)]


def test_shadow_gate_without_a_face_claim_sends_nothing():
    base = DispatchRouter(routes={})
    r = router_for_skills(None, base=base)
    t = _Track(1)
    fd = _FakeDispatcher()
    assert dispatch_escalations("cam2", [t], _gate(t, shadow=True), r, fd, follow=FaceFollowUp()) == 0
    assert fd.calls == []


def test_enforce_runs_defaults_beside_the_face_route_once_each():
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(),
                          skill_adapters=FACE_CLAIM)
    t = _Track(1)
    fd = _FakeDispatcher()
    dispatch_escalations("cam1", [t], _gate(t, shadow=False), r, fd, follow=FaceFollowUp(), now=10.0)
    assert sorted(c[1] for c in fd.calls) == ["caption", FACE]


def test_an_unescalated_person_is_not_sent():
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(routes={}),
                          skill_adapters=FACE_CLAIM)
    t = _Track(1)
    fd = _FakeDispatcher()
    dispatch_escalations("cam1", [t], _gate(t, shadow=True, escalate=False), r, fd, follow=FaceFollowUp())
    assert follow_up_faces("cam1", [t], r, fd, FaceFollowUp()) == 0
    assert fd.calls == []


# -- follow-up --------------------------------------------------------------

def _started(follow, r, t, fd, now=10.0):
    dispatch_escalations("cam1", [t], _gate(t, shadow=True), r, fd, follow=follow, now=now)


def test_a_person_is_resent_when_their_crop_improves():
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(routes={}),
                          skill_adapters=FACE_CLAIM)
    follow, fd, t = FaceFollowUp(min_interval_s=0.5), _FakeDispatcher(), _Track(7, crop_ts=1.0)
    _started(follow, r, t, fd)
    assert follow_up_faces("cam1", [t], r, fd, follow, now=11.0) == 0   # same crop
    t.best_crop_ts = 2.0
    assert follow_up_faces("cam1", [t], r, fd, follow, now=10.2) == 0   # too soon
    assert follow_up_faces("cam1", [t], r, fd, follow, now=11.0) == 1
    assert len(fd.calls) == 2


def test_follow_up_stops_once_recognised():
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(routes={}),
                          skill_adapters=FACE_CLAIM)
    follow, fd, t = FaceFollowUp(min_interval_s=0.0), _FakeDispatcher(), _Track(7)
    _started(follow, r, t, fd)
    fd.last_on_result(7, FACE, {"result": {"recognized": True, "person_id": "S1", "similarity": 0.7}})
    t.best_crop_ts = 5.0
    assert follow_up_faces("cam1", [t], r, fd, follow, now=20.0) == 0


def test_an_unknown_face_is_retried_up_to_the_cap():
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(routes={}),
                          skill_adapters=FACE_CLAIM)
    follow, fd, t = FaceFollowUp(max_attempts=3, min_interval_s=0.0), _FakeDispatcher(), _Track(7)
    _started(follow, r, t, fd)
    for i in range(10):
        fd.last_on_result(7, FACE, {"result": {"recognized": False, "similarity": 0.3}})
        t.best_crop_ts += 1
        follow_up_faces("cam1", [t], r, fd, follow, now=30.0 + i)
    assert len(fd.calls) == 3


def test_a_dropped_dispatch_does_not_spend_an_attempt():
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(routes={}),
                          skill_adapters=FACE_CLAIM)
    follow, t = FaceFollowUp(), _Track(7)
    _started(follow, r, t, _FakeDispatcher(accept=False))
    assert not follow.started(7, FACE)


def test_face_recognised_reads_both_envelopes():
    assert face_recognised({"result": {"recognized": True, "person_id": "A"}})
    assert face_recognised({"recognized": True, "person_id": "A"})
    assert not face_recognised({"result": {"recognized": True, "person_id": None}})
    assert not face_recognised(None)


# -- provider and startup ---------------------------------------------------

def _cam(assignments):
    return {"camera_id": "cam5", "open_nvr_camera_id": 5, "frame_url": "rtsp://x/y",
            "assignments": assignments}


def test_provider_reads_the_claimed_adapters_into_the_spec():
    c = _cam([{"skill": "face_recognition", "adapters": [FACE]}, {"skill": "object_detection"}])
    assert _skill_adapters(c) == FACE_CLAIM
    spec = _to_spec(c)
    assert spec.skill_adapters == FACE_CLAIM and "face_recognition" in spec.skills
    assert _skill_adapters(_cam([{"skill": "face_recognition"}])) is None
    assert _skill_adapters({"camera_id": "cam1"}) is None


def test_build_dispatch_per_gate_mode():
    from detect_pipeline.run import _build_dispatch

    def cfg(url, mode, face=""):
        return SimpleNamespace(dispatch_kaic_url=url, gate_mode=mode, api_key=None,
                               dispatch_task="caption", dispatch_max_inflight=2, face_kaic_url=face)

    assert _build_dispatch(cfg("", "enforce")) == (None, None)
    # The face URL alone: the face route only, even in enforce (no caption
    # suddenly appearing on a site that was already enforcing).
    df, rf = _build_dispatch(cfg("", "enforce", face="http://opennvr-core:8100"))
    assert df is not None and rf.route("person") == [] and rf.route("car") == []
    df.close()
    d, r = _build_dispatch(cfg("http://kai-c:8100", "shadow"))
    assert d is not None and r.route("person") == [] and r.route("car") == []
    d2, r2 = _build_dispatch(cfg("http://kai-c:8100", "enforce"))
    assert r2.route("person") == ["caption"]
    d.close(); d2.close()


def test_a_face_only_camera_is_not_skipped(monkeypatch):
    from detect_pipeline.providers import _assignment_view
    monkeypatch.setenv("DETECT_SKIP_UNASSIGNED", "true")
    _labels, analyze, _skills, _extra = _assignment_view(_cam([{"skill": "face_recognition", "adapters": [FACE]}]))
    assert analyze


# -- a camera that stays active (a class running) ---------------------------

def _spent(follow, r, t, fd, attempts=2):
    """Start a track and spend its attempts on unknown faces."""
    _started(follow, r, t, fd)
    for i in range(attempts * 2):
        fd.last_on_result(t.id, FACE, {"result": {"recognized": False}})
        t.best_crop_ts += 1
        follow_up_faces("cam1", [t], r, fd, follow, now=11.0 + i)
    return len(fd.calls)


def test_an_unnamed_person_is_retried_on_fresh_crops_while_the_camera_stays_active():
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(routes={}),
                          skill_adapters=FACE_CLAIM)
    follow = FaceFollowUp(max_attempts=2, min_interval_s=0.0, retry_every_s=6.0)
    fd, t = _FakeDispatcher(), _Track(7)
    assert _spent(follow, r, t, fd) == 2
    crops = []

    def fresh(track):
        crops.append(track.id)
        return "fresh-crop"

    # No better best crop, but a fresh one of the current frame is tried
    # every retry_every_s, and only where the caller offers one.
    assert follow_up_faces("cam1", [t], r, fd, follow, now=15.0, fresh_crop=fresh) == 0
    assert follow_up_faces("cam1", [t], r, fd, follow, now=30.0) == 0
    assert follow_up_faces("cam1", [t], r, fd, follow, now=30.0, fresh_crop=fresh) == 1
    assert follow_up_faces("cam1", [t], r, fd, follow, now=33.0, fresh_crop=fresh) == 0
    assert follow_up_faces("cam1", [t], r, fd, follow, now=36.0, fresh_crop=fresh) == 1
    assert crops == [7, 7] and len(fd.calls) == 4
    # A name ends it.
    fd.last_on_result(7, FACE, {"result": {"recognized": True, "person_id": "S1"}})
    assert follow_up_faces("cam1", [t], r, fd, follow, now=99.0, fresh_crop=fresh) == 0


def test_a_person_out_of_view_is_not_retried():
    r = router_for_skills(frozenset({"face_recognition"}), base=DispatchRouter(routes={}),
                          skill_adapters=FACE_CLAIM)
    follow = FaceFollowUp(max_attempts=1, min_interval_s=0.0, retry_every_s=6.0)
    fd, t = _FakeDispatcher(), _Track(7)
    _spent(follow, r, t, fd, attempts=1)
    t.misses = 3          # the tracker is coasting on a box nobody stands in
    assert follow_up_faces("cam1", [t], r, fd, follow, now=60.0,
                           fresh_crop=lambda tr: "crop") == 0
    t.misses = 0
    assert follow_up_faces("cam1", [t], r, fd, follow, now=60.0,
                           fresh_crop=lambda tr: None) == 0      # nothing to cut
    assert follow_up_faces("cam1", [t], r, fd, follow, now=60.0,
                           fresh_crop=lambda tr: "crop") == 1


def test_provider_reads_the_stay_active_lease():
    assert _to_spec({**_cam([]), "stay_active": True}).stay_active is True
    assert _to_spec({**_cam([]), "stay_active": "yes"}).stay_active is False
    assert _to_spec(_cam([])).stay_active is False          # older core
