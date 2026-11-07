# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""attendance/core.py on a synthetic clock: no camera, no model, no core."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from attendance.core import Planner, attempt_payload, matches_from, parse_active

T0 = datetime(2026, 11, 2, 3, 30, tzinfo=UTC)


def _s(sid=1, cams=(1,), people=("a", "b"), start=0, end=50, interval=150, bucket=5, thr=0.5):
    return {"session_id": sid, "camera_ids": list(cams), "person_ids": list(people),
            "start_at": (T0 + timedelta(minutes=start)).isoformat(),
            "end_at": (T0 + timedelta(minutes=end)).isoformat(),
            "attempt_interval_s": interval, "bucket_minutes": bucket, "match_threshold": thr}


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_parse_active_tells_no_answer_from_nothing_running():
    assert parse_active(None) is None
    assert parse_active({"error": "x"}) is None
    assert parse_active({"sessions": []}) == []
    [s] = parse_active({"sessions": [_s(), {"session_id": 2}, "junk"]})
    assert s.session_id == 1 and s.camera_ids == (1,) and s.person_ids == {"a", "b"}


def test_one_look_per_camera_even_when_two_sessions_share_it():
    p = Planner()
    sessions = parse_active({"sessions": [_s(1, cams=(1, 2)), _s(2, cams=(1,), people=("z",))]})
    work = p.due(sessions, _at(1))
    assert [(w.camera_id, [s.session_id for s in w.sessions]) for w in work] == [
        (1, [1, 2]), (2, [1])]
    assert work[0].members == {"a", "b", "z"}


def test_each_camera_waits_its_interval():
    p = Planner()
    sessions = parse_active({"sessions": [_s(interval=150)]})
    [w] = p.due(sessions, _at(1))
    p.looked(w.camera_id, _at(1))
    assert p.due(sessions, _at(2)) == []                 # 60 s later
    assert len(p.due(sessions, _at(3.5))) == 1           # 150 s later


def test_the_shortest_interval_of_a_shared_camera_wins():
    p = Planner()
    sessions = parse_active({"sessions": [_s(1, interval=300), _s(2, interval=60, people=("z",))]})
    [w] = p.due(sessions, _at(1))
    assert w.interval_s == 60


def test_sessions_outside_their_window_are_not_watched_and_cameras_forgotten():
    p = Planner()
    sessions = parse_active({"sessions": [_s(start=0, end=50)]})
    p.looked(1, _at(1))
    assert p.due(sessions, _at(51)) == [] and p.last_attempt == {}
    assert p.due(parse_active({"sessions": [_s(start=10, end=50)]}), _at(5)) == []


def test_evidence_once_per_camera_per_bucket():
    p = Planner()
    [w] = p.due(parse_active({"sessions": [_s(bucket=5)]}), _at(1))
    assert p.evidence_due(w, _at(1)) is True
    assert p.evidence_due(w, _at(4)) is False             # same 5-minute bucket
    assert p.evidence_due(w, _at(6)) is True              # next bucket


def test_only_members_above_threshold_are_reported_best_score_each():
    [s] = parse_active({"sessions": [_s(people=("a", "b"), thr=0.55)]})
    w = Planner().due([s], _at(1))[0]
    result = {"faces": [
        {"recognized": True, "person_id": "a", "similarity": 0.6, "bbox": [1, 1, 2, 2]},
        {"recognized": True, "person_id": "a", "similarity": 0.8, "bbox": [3, 3, 4, 4]},
        {"recognized": True, "person_id": "b", "similarity": 0.54},       # below threshold
        {"recognized": True, "person_id": "stranger", "similarity": 0.99},
        {"recognized": False, "person_id": None, "similarity": 0.3},
    ]}
    assert matches_from(result, w) == [{"person_id": "a", "similarity": 0.8, "bbox": [3, 3, 4, 4]}]


def test_attempt_payload_matches_the_contract():
    [s] = parse_active({"sessions": [_s()]})
    w = Planner().due([s], _at(1))[0]
    ok = attempt_payload(w, _at(1), ok=True, evidence_path="ab/x.jpg", result={
        "detected_count": 31, "faces": [{"recognized": True, "person_id": "a", "similarity": 0.7}]})
    assert ok == {"at": _at(1).isoformat(), "ok": True, "faces_detected": 31,
                  "matches": [{"person_id": "a", "similarity": 0.7, "bbox": None}],
                  "session_ids": [1], "evidence_path": "ab/x.jpg"}
    failed = attempt_payload(w, _at(1), ok=False)
    assert failed["ok"] is False and failed["matches"] == [] and failed["faces_detected"] == 0

    from opennvr_app_sdk import AttendanceAttempt
    assert AttendanceAttempt.from_payload(ok).to_payload() == ok
