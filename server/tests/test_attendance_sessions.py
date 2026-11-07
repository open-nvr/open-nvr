# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Attendance sessions: the scheduler, the finalizer, the attempt consumer.

On a synthetic clock, against the shared API fixture:

* the timetable materialises once per (slot, local date), in the group's
  timezone; a scheduled session follows its slot, a started one does not;
* buckets with no frame looked at are left out of everyone's ratio, so a
  camera outage is a gap and not a class marked absent;
* PRESENT / LATE / ABSENT follow the policy; manual marks survive a
  re-finalise and AUTO hands them back;
* the consumer links one attempt to every session on its camera, keeps
  only members above threshold, and is idempotent;
* a running session with nothing watched raises one coverage alert per gap;
* the app's /active answers by time, for its own cameras only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from services import (
    attendance_event_consumer as consumer,
    attendance_faces,
    attendance_scheduler as sched,
)
from tests.test_api_tokens import env  # noqa: F401 - shared fixture

URL = "/api/v1/attendance"
MONDAY = date(2026, 11, 2)
#: 09:00 IST on that Monday.
T0 = datetime(2026, 11, 2, 3, 30, tzinfo=UTC)


def _min(m: float) -> datetime:
    return T0 + timedelta(minutes=m)


def _enrolled(env, *people, when=None):  # noqa: F811
    """Consent on record and a face enrolled, written straight to the DB:
    only such people can be recognised, so only they are marked."""
    when = when or datetime(2026, 1, 1, tzinfo=UTC)
    s = env.Session()
    for p in people:
        row = s.get(env.models.AttendancePerson, p["id"])
        row.consent_at, row.face_samples = when, 1
        # Joined before the (fake-clock) classes these tests run.
        s.query(env.models.AttendanceGroupMember).filter(
            env.models.AttendanceGroupMember.person_id == p["id"]).update({"added_at": when})
    s.commit()
    s.close()


def _setup(env, members=("Asha", "Bilal", "Chen", "Dev"), cams=(1,), **slot):  # noqa: F811
    A = env.jwt("admin")
    g = env.client.post(f"{URL}/groups", headers=A,
                        json={"name": "CS-A", "timezone": "Asia/Kolkata"}).json()
    people = {}
    for i, name in enumerate(members):
        people[name] = env.client.post(f"{URL}/people", headers=A, json={
            "display_name": name, "external_ref": f"R{i}"}).json()
    env.client.put(f"{URL}/groups/{g['id']}/members", headers=A,
                   json={"person_ids": [p["id"] for p in people.values()]})
    _enrolled(env, *people.values())
    body = {"group_id": g["id"], "weekday": 0, "start_time": "09:00", "end_time": "09:50",
            "camera_ids": list(cams), **slot}
    s = env.client.post(f"{URL}/timetable", headers=A, json=body)
    assert s.status_code == 201, s.text
    return g, people, s.json()


def _tick(env, at):  # noqa: F811
    db = env.Session()
    try:
        return sched.tick(db, at)
    finally:
        db.close()


def _sessions(env):  # noqa: F811
    db = env.Session()
    rows = db.query(env.models.AttendanceSession).order_by(env.models.AttendanceSession.id).all()
    db.expunge_all()
    db.close()
    return rows


_seq = iter(range(1, 10**9))


def _attempt(env, at, matches=(), cam=1, ok=True, **extra):  # noqa: F811
    env_id = extra.pop("id", f"evt-{next(_seq)}")
    envelope = {"id": env_id, "schema": "attendance.attempt.v1", "producer": "attendance",
                "camera_id": f"cam{cam}", "ts": at.isoformat(),
                "payload": {"at": at.isoformat(), "ok": ok, "faces_detected": len(matches),
                            "matches": [{"person_id": p["uuid"], "similarity": sim,
                                         "bbox": [1, 2, 3, 4]} for p, sim in matches]}}
    envelope.update(extra)
    db = env.Session()
    try:
        return consumer.apply_attempt_event(envelope, db, now=at + timedelta(seconds=1))
    finally:
        db.close()


def _marks(env, session_id):  # noqa: F811
    detail = env.client.get(f"{URL}/sessions/{session_id}", headers=env.jwt("admin")).json()
    return {p["display_name"]: p["status"] for p in detail["people"]}, detail


# ── Materialising ────────────────────────────────────────────────────


def test_the_timetable_becomes_one_session_per_slot_and_local_date(env):  # noqa: F811
    _setup(env)
    # Sunday 23:00 IST: Monday is "tomorrow" in Kolkata, still Sunday in UTC.
    sunday_night = datetime(2026, 11, 1, 17, 30, tzinfo=UTC)
    assert _tick(env, sunday_night)["materialize"]["created"] == 1
    assert _tick(env, sunday_night)["materialize"]["created"] == 0
    [s] = _sessions(env)
    assert s.local_date == MONDAY and s.status == "scheduled"
    assert sched._utc(s.start_at) == T0 and sched._utc(s.end_at) == _min(50)


def test_a_class_already_over_is_not_invented_at_boot(env):  # noqa: F811
    _setup(env)
    _tick(env, _min(60))         # booted after the bell
    assert _sessions(env) == []


def test_a_scheduled_session_follows_its_slot_and_a_started_one_does_not(env):  # noqa: F811
    g, _, slot = _setup(env)
    _tick(env, _min(-60))
    body = {"group_id": g["id"], "weekday": 0, "start_time": "09:10", "end_time": "09:50",
            "camera_ids": [2]}
    env.client.put(f"{URL}/timetable/{slot['id']}", headers=env.jwt("admin"), json=body)
    _tick(env, _min(-30))
    [s] = _sessions(env)
    assert sched._utc(s.start_at) == _min(10) and s.camera_ids == [2]

    _tick(env, _min(15))         # started
    env.client.put(f"{URL}/timetable/{slot['id']}", headers=env.jwt("admin"),
                   json={**body, "camera_ids": [3]})
    _tick(env, _min(16))
    assert _sessions(env)[0].camera_ids == [2] and _sessions(env)[0].status == "running"


def test_switching_a_slot_off_cancels_its_scheduled_session(env):  # noqa: F811
    g, _, slot = _setup(env)
    _tick(env, _min(-60))
    env.client.put(f"{URL}/timetable/{slot['id']}", headers=env.jwt("admin"), json={
        "group_id": g["id"], "weekday": 0, "start_time": "09:00", "end_time": "09:50",
        "camera_ids": [1], "active": False})
    _tick(env, _min(-30))
    assert _sessions(env)[0].status == "cancelled"


def test_deleting_a_slot_cancels_its_future_session(env):  # noqa: F811
    _, _, slot = _setup(env)
    _tick(env, _min(-60))
    env.client.delete(f"{URL}/timetable/{slot['id']}", headers=env.jwt("admin"))
    [s] = _sessions(env)
    assert s.status == "cancelled" and s.slot_id is None


def test_a_term_ends_with_valid_to(env):  # noqa: F811
    _setup(env, valid_to="2026-11-01")
    _tick(env, _min(-60))
    assert _sessions(env) == []


# ── Finalising ───────────────────────────────────────────────────────


def test_present_late_absent_and_an_outage_that_is_not_absence(env):  # noqa: F811
    """50 minutes = 10 buckets of 5. The camera is watched for the first
    six only (then it dies): those are the ones that count."""
    _, p, _ = _setup(env)
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    asha, bilal, chen = p["Asha"], p["Bilal"], p["Chen"]
    for bucket in range(6):
        at = _min(bucket * 5 + 1)
        who = [(asha, 0.8)] if bucket < 5 else []          # 5 of 6 → PRESENT
        if bucket >= 3:
            who.append((bilal, 0.7))                        # 3 of 6 → ABSENT
        if bucket >= 2:
            who.append((chen, 0.6))                         # 4 of 6, from 10:11 → LATE
        assert _attempt(env, at, who) == "applied"
    # Dev is never seen. A frame that failed is no coverage either.
    _attempt(env, _min(31), ok=False)

    _tick(env, _min(50))                 # ends
    assert _sessions(env)[0].status == "finalizing"
    _tick(env, _min(55))                 # grace over
    marks, detail = _marks(env, _sessions(env)[0].id)
    assert marks == {"Asha": "PRESENT", "Bilal": "ABSENT", "Chen": "LATE", "Dev": "ABSENT"}
    assert detail["status"] == "final" and detail["total_buckets"] == 10
    assert detail["counted_buckets"] == 6 and detail["no_coverage"] is False
    assert detail["counts"] == {"PRESENT": 1, "LATE": 1, "ABSENT": 2}


def test_nothing_watched_is_no_coverage(env):  # noqa: F811
    _setup(env)
    _tick(env, _min(-60))
    _tick(env, _min(1))
    _tick(env, _min(56))
    marks, detail = _marks(env, _sessions(env)[0].id)
    assert detail["no_coverage"] is True and set(marks.values()) == {"ABSENT"}


def test_group_policy_changes_the_verdict(env):  # noqa: F811
    g, p, _ = _setup(env, members=("Asha",))
    env.client.patch(f"{URL}/groups/{g['id']}", headers=env.jwt("admin"),
                     json={"policy": {"present_ratio": 0.4}})
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    for bucket in range(10):
        _attempt(env, _min(bucket * 5 + 1), [(p["Asha"], 0.9)] if bucket < 5 else [])
    _tick(env, _min(56))
    assert _marks(env, _sessions(env)[0].id)[0] == {"Asha": "PRESENT"}   # 5/10 ≥ 0.4


def test_manual_marks_win_and_auto_hands_them_back(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha", "Dev"))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    _attempt(env, _min(1), [(p["Asha"], 0.9)])
    _tick(env, _min(56))
    sid = _sessions(env)[0].id
    A = env.jwt("admin")
    r = env.client.patch(f"{URL}/sessions/{sid}/marks/{p['Dev']['id']}", headers=A,
                         json={"status": "excused", "note": "medical"})
    assert r.status_code == 200 and r.json() == {
        "session_id": sid, "person_id": p["Dev"]["id"], "status": "EXCUSED", "source": "manual"}
    assert env.client.post(f"{URL}/sessions/{sid}/refinalize", headers=A).status_code == 200
    assert _marks(env, sid)[0]["Dev"] == "EXCUSED"

    env.client.patch(f"{URL}/sessions/{sid}/marks/{p['Dev']['id']}", headers=A,
                     json={"status": "AUTO"})
    assert _marks(env, sid)[0]["Dev"] == "ABSENT"


def test_marks_cannot_be_set_before_the_verdict_or_for_strangers(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    sid = _sessions(env)[0].id
    A = env.jwt("admin")
    assert env.client.patch(f"{URL}/sessions/{sid}/marks/{p['Asha']['id']}", headers=A,
                            json={"status": "PRESENT"}).status_code == 409
    _tick(env, _min(56))
    assert env.client.patch(f"{URL}/sessions/{sid}/marks/999", headers=A,
                            json={"status": "PRESENT"}).status_code == 404
    assert env.client.patch(f"{URL}/sessions/{sid}/marks/{p['Asha']['id']}", headers=A,
                            json={"status": "MAYBE"}).status_code == 422


# ── The consumer ─────────────────────────────────────────────────────


def test_one_attempt_serves_every_session_on_its_camera(env):  # noqa: F811
    """Two groups in one room at once: one frame, both sessions."""
    _, p1, _ = _setup(env, members=("Asha",))
    A = env.jwt("admin")
    g2 = env.client.post(f"{URL}/groups", headers=A,
                         json={"name": "CS-B", "timezone": "Asia/Kolkata"}).json()
    zed = env.client.post(f"{URL}/people", headers=A,
                          json={"display_name": "Zed", "external_ref": "Z"}).json()
    env.client.put(f"{URL}/groups/{g2['id']}/members", headers=A, json={"person_ids": [zed["id"]]})
    env.client.post(f"{URL}/timetable", headers=A, json={
        "group_id": g2["id"], "weekday": 0, "start_time": "09:00", "end_time": "09:50",
        "camera_ids": [1]})
    _tick(env, _min(-60))
    _tick(env, _min(0.5))

    assert _attempt(env, _min(1), [(p1["Asha"], 0.9), (zed, 0.9)]) == "applied"
    db = env.Session()
    m = env.models
    assert db.query(m.AttendanceAttempt).count() == 1
    assert db.query(m.AttendanceAttemptSession).count() == 2
    # Each session keeps only its own member.
    pairs = {(s.session_id, s.person_id) for s in db.query(m.AttendanceSighting).all()}
    db.close()
    s1, s2 = (s.id for s in _sessions(env))
    assert pairs == {(s1, p1["Asha"]["id"]), (s2, zed["id"])}


def test_matches_below_threshold_or_outside_the_group_are_dropped(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha", "Bilal"))
    stranger = env.client.post(f"{URL}/people", headers=env.jwt("admin"),
                               json={"display_name": "Visitor", "external_ref": "V"}).json()
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    _attempt(env, _min(1), [(p["Asha"], 0.49), (p["Bilal"], 0.51), (stranger, 0.99),
                            ({"uuid": "not-a-person"}, 0.99)])
    db = env.Session()
    seen = {s.person_id for s in db.query(env.models.AttendanceSighting).all()}
    attempt = db.query(env.models.AttendanceAttempt).one()
    db.close()
    assert seen == {p["Bilal"]["id"]} and attempt.matched == 1


def test_the_consumer_is_idempotent_and_refuses_what_it_cannot_place(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    assert _attempt(env, _min(1), [(p["Asha"], 0.9)], id="same") == "applied"
    assert _attempt(env, _min(1), [(p["Asha"], 0.9)], id="same") == "duplicate"
    assert _attempt(env, _min(1), cam=2) == "no_session"
    assert _attempt(env, _min(70)) == "no_session"
    assert _attempt(env, _min(1), schema="screening.completed.v1") == "foreign"
    # producer is the publisher's own free text, so it is not a gate.
    assert _attempt(env, _min(2), producer="app:attendance") == "applied"
    assert _attempt(env, _min(1), camera_id="garage") == "malformed"
    db = env.Session()
    future = consumer.apply_attempt_event(
        {"id": "f", "camera_id": "cam1", "payload": {"at": _min(30).isoformat()}}, db,
        now=_min(1))
    db.close()
    assert future == "future"


def test_an_attempt_after_the_verdict_changes_nothing(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    _tick(env, _min(56))                       # final, no coverage
    assert _attempt(env, _min(10), [(p["Asha"], 0.9)]) == "no_session"


def test_old_attempts_are_pruned_but_marks_stay(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    _attempt(env, _min(1), [(p["Asha"], 0.9)])
    _tick(env, _min(56))
    db = env.Session()
    assert consumer.prune(db, now=_min(56) + timedelta(days=consumer.RETENTION_DAYS + 1)) == 1
    m = env.models
    assert db.query(m.AttendanceSighting).count() == 0
    assert db.query(m.AttendanceMark).one().status == "PRESENT"
    db.close()


# ── Coverage alerts ──────────────────────────────────────────────────


def test_a_session_nobody_is_watching_raises_one_alert_per_gap(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    _attempt(env, _min(1), [(p["Asha"], 0.9)])
    assert _tick(env, _min(9))["coverage_alerts"] == 0          # too early
    assert _tick(env, _min(12))["coverage_alerts"] == 1         # 11 min since the last frame
    assert _tick(env, _min(12.5))["coverage_alerts"] == 0       # same gap, same alert
    db = env.Session()
    alert = db.query(env.models.AppAlert).one()
    db.close()
    assert "CS-A" in alert.title and alert.severity == "medium"


# ── The app's view ───────────────────────────────────────────────────


@pytest.fixture()
def internal(env):  # noqa: F811
    from core.config import settings
    from routers import attendance

    env.client.app.include_router(attendance.internal_router, prefix="/api/v1")
    return {"X-Internal-Api-Key": settings.internal_api_key}


def test_active_answers_by_time_with_the_members_to_look_for(env, internal, monkeypatch):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha", "Bilal"), cams=(1, 2))
    _tick(env, _min(-60))
    removed = p["Bilal"]

    async def forgotten(_uuid):
        return True

    monkeypatch.setattr(attendance_faces, "forget", forgotten)
    assert env.client.delete(f"{URL}/people/{removed['id']}",
                             headers=env.jwt("admin")).status_code == 200

    import routers.attendance as ra

    class _Clock(datetime):
        current = _min(1)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(ra, "datetime", _Clock)
    # Still "scheduled" (no tick since the bell), yet already active.
    [s] = env.client.get("/api/v1/internal/app/attendance/active",
                         headers=internal).json()["sessions"]
    assert s["camera_ids"] == [1, 2] and s["person_ids"] == [p["Asha"]["uuid"]]
    assert s["attempt_interval_s"] == 150 and s["match_threshold"] == 0.5

    _Clock.current = _min(51)
    assert env.client.get("/api/v1/internal/app/attendance/active",
                          headers=internal).json()["sessions"] == []


def test_active_is_scoped_to_the_apps_cameras_and_refuses_users(env, internal, monkeypatch):  # noqa: F811
    _setup(env, cams=(1, 2))
    _tick(env, _min(-60))
    import routers.attendance as ra

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _min(1)

    monkeypatch.setattr(ra, "datetime", _Clock)
    monkeypatch.setattr(ra, "_app_roster", lambda db, principal: {2})
    [s] = env.client.get("/api/v1/internal/app/attendance/active",
                         headers=internal).json()["sessions"]
    assert s["camera_ids"] == [2]
    monkeypatch.setattr(ra, "_app_roster", lambda db, principal: {3})
    assert env.client.get("/api/v1/internal/app/attendance/active",
                          headers=internal).json()["sessions"] == []
    assert env.client.get("/api/v1/internal/app/attendance/active",
                          headers=env.jwt("admin")).status_code == 401


# ── Site policy ──────────────────────────────────────────────────────


def test_site_policy_round_trip_and_its_checks(env):  # noqa: F811
    A = env.jwt("admin")
    assert env.client.get(f"{URL}/policy", headers=A).json()["policy"]["bucket_minutes"] == 5
    r = env.client.put(f"{URL}/policy", headers=A, json={"present_ratio": 0.75})
    assert r.status_code == 200 and r.json()["policy"]["present_ratio"] == 0.75
    assert env.client.get(f"{URL}/policy", headers=A).json()["policy"]["present_ratio"] == 0.75
    assert env.client.put(f"{URL}/policy", headers=A,
                          json={"attempt_interval_s": 200}).status_code == 422

    # A group's override must still make sense on top of the new site policy.
    assert env.client.put(f"{URL}/policy", headers=A,
                          json={"attempt_interval_s": 120}).status_code == 200
    g = env.client.post(f"{URL}/groups", headers=A, json={
        "name": "Short", "timezone": "UTC", "policy": {"bucket_minutes": 4}})
    assert g.status_code == 201, g.text
    # Fine for the site's own 5-minute buckets, but 150 s in this group's
    # 4-minute ones is one chance per bucket: refused, naming the problem.
    r = env.client.put(f"{URL}/policy", headers=A, json={"attempt_interval_s": 150})
    assert r.status_code == 422 and "half of bucket_minutes" in r.text


# ── Review findings (2026-11-04) ─────────────────────────────────────


def test_a_slot_switched_off_and_on_again_brings_its_session_back(env):  # noqa: F811
    g, _, slot = _setup(env)
    body = {"group_id": g["id"], "weekday": 0, "start_time": "09:00", "end_time": "09:50",
            "camera_ids": [1]}
    A = env.jwt("admin")
    _tick(env, _min(-60))
    env.client.put(f"{URL}/timetable/{slot['id']}", headers=A, json={**body, "active": False})
    _tick(env, _min(-50))
    assert _sessions(env)[0].status == "cancelled"
    env.client.put(f"{URL}/timetable/{slot['id']}", headers=A, json={**body, "active": True})
    _tick(env, _min(-40))
    assert _sessions(env)[0].status == "scheduled"
    _tick(env, _min(1))
    assert _sessions(env)[0].status == "running"


def test_a_cancelled_session_whose_start_has_passed_stays_cancelled(env):  # noqa: F811
    g, _, slot = _setup(env)
    body = {"group_id": g["id"], "weekday": 0, "start_time": "09:00", "end_time": "09:50",
            "camera_ids": [1]}
    A = env.jwt("admin")
    _tick(env, _min(-60))
    env.client.put(f"{URL}/timetable/{slot['id']}", headers=A, json={**body, "active": False})
    _tick(env, _min(-50))
    env.client.put(f"{URL}/timetable/{slot['id']}", headers=A, json={**body, "active": True})
    _tick(env, _min(5))                        # back on, but after the bell
    assert _sessions(env)[0].status == "cancelled"


def test_a_period_inside_a_dst_gap_is_skipped(env):  # noqa: F811
    A = env.jwt("admin")
    g = env.client.post(f"{URL}/groups", headers=A,
                        json={"name": "NY", "timezone": "America/New_York"}).json()
    # 8 March 2026 is a Sunday: 02:00 does not exist in New York, and
    # zoneinfo puts it on the same instant as 03:00 - a zero-length period.
    # (02:00-02:59 would shift to a real hour, and that one is kept.)
    env.client.post(f"{URL}/timetable", headers=A, json={
        "group_id": g["id"], "weekday": 6, "start_time": "02:00", "end_time": "03:00",
        "camera_ids": [1]})
    _tick(env, datetime(2026, 3, 8, 5, 0, tzinfo=UTC))     # 00:00 EST that day
    assert _sessions(env) == []


def test_recompute_is_refused_once_the_evidence_is_pruned(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    _attempt(env, _min(1), [(p["Asha"], 0.9)])
    _tick(env, _min(56))
    sid = _sessions(env)[0].id
    db = env.Session()
    consumer.prune(db, now=_min(56) + timedelta(days=consumer.RETENTION_DAYS + 1))
    s = db.get(env.models.AttendanceSession, sid)
    s.start_at, s.end_at = (_min(0) - timedelta(days=consumer.RETENTION_DAYS + 1),
                            _min(50) - timedelta(days=consumer.RETENTION_DAYS + 1))
    db.commit()
    db.close()
    A = env.jwt("admin")
    r = env.client.post(f"{URL}/sessions/{sid}/refinalize", headers=A)
    assert r.status_code == 409 and "retention" in r.text
    r = env.client.patch(f"{URL}/sessions/{sid}/marks/{p['Asha']['id']}", headers=A,
                         json={"status": "AUTO"})
    assert r.status_code == 409
    assert _marks(env, sid)[0] == {"Asha": "PRESENT"}      # the record survived


def test_auto_hands_back_one_mark_and_leaves_the_others(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha", "Dev"))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    _attempt(env, _min(1), [(p["Asha"], 0.9)])
    _tick(env, _min(56))
    sid = _sessions(env)[0].id
    A = env.jwt("admin")
    # Somebody joins the group after the session.
    late = env.client.post(f"{URL}/people", headers=A,
                           json={"display_name": "Newcomer", "external_ref": "N1"}).json()
    env.client.post(f"{URL}/groups/{_sessions(env)[0].group_id}/members", headers=A,
                    json={"person_ids": [late["id"]]})
    env.client.patch(f"{URL}/sessions/{sid}/marks/{p['Dev']['id']}", headers=A,
                     json={"status": "EXCUSED"})
    env.client.patch(f"{URL}/sessions/{sid}/marks/{p['Dev']['id']}", headers=A,
                     json={"status": "AUTO"})
    marks = _marks(env, sid)[0]
    assert marks["Dev"] == "ABSENT" and marks["Asha"] == "PRESENT"
    assert marks["Newcomer"] is None           # not given a mark for a class before they joined


def test_a_look_whose_ok_is_the_string_false_counts_for_nothing(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    db = env.Session()
    status = consumer.apply_attempt_event(
        {"id": "str-false", "schema": "attendance.attempt.v1", "camera_id": "cam1",
         "payload": {"at": _min(1).isoformat(), "ok": "false",
                     "matches": [{"person_id": p["Asha"]["uuid"], "similarity": 0.9}]}},
        db, now=_min(1))
    attempt = db.query(env.models.AttendanceAttempt).one()
    sightings = db.query(env.models.AttendanceSighting).count()
    db.close()
    assert status == "applied" and attempt.ok is False and sightings == 0


def test_finalize_grace_is_at_least_a_minute(env):  # noqa: F811
    A = env.jwt("admin")
    assert env.client.put(f"{URL}/policy", headers=A,
                          json={"finalize_grace_minutes": 0}).status_code == 422


# ── Who is marked, coverage, calling a session off, one-off sessions ──


def test_only_people_a_camera_could_recognise_are_marked(env):  # noqa: F811
    """No consent, no face, inactive: unknown to the camera, so ABSENT
    would not be evidence of anything. They get no mark at all."""
    _, p, _ = _setup(env, members=("Asha", "NoConsent", "NoFace", "Inactive"))
    s = env.Session()
    s.get(env.models.AttendancePerson, p["NoConsent"]["id"]).consent_at = None
    s.get(env.models.AttendancePerson, p["NoFace"]["id"]).face_samples = 0
    s.get(env.models.AttendancePerson, p["Inactive"]["id"]).active = False
    s.commit()
    s.close()
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    for bucket in range(10):
        _attempt(env, _min(bucket * 5 + 1), [(p["Asha"], 0.9)])
    _tick(env, _min(56))
    marks, _ = _marks(env, _sessions(env)[0].id)
    assert marks == {"Asha": "PRESENT", "NoConsent": None, "NoFace": None, "Inactive": None}


def test_a_recompute_keeps_the_roster_the_session_was_decided_for(env):  # noqa: F811
    g, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    for bucket in range(10):
        _attempt(env, _min(bucket * 5 + 1), [(p["Asha"], 0.9)])
    _tick(env, _min(56))
    sid = _sessions(env)[0].id
    A = env.jwt("admin")
    late = env.client.post(f"{URL}/people", headers=A,
                           json={"display_name": "Joined later", "external_ref": "L1"}).json()
    env.client.post(f"{URL}/groups/{g['id']}/members", headers=A, json={"person_ids": [late["id"]]})
    _enrolled(env, late)
    assert env.client.post(f"{URL}/sessions/{sid}/refinalize", headers=A).status_code == 200
    marks, _ = _marks(env, sid)
    assert marks == {"Asha": "PRESENT", "Joined later": None}


def test_a_session_watched_too_little_is_flagged_and_not_counted(env):  # noqa: F811
    """Two of ten buckets watched (the camera died): below the 0.5 floor,
    so the session says so instead of passing for a full one."""
    _, p, _ = _setup(env, members=("Asha", "Bilal"))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    for bucket in range(2):
        _attempt(env, _min(bucket * 5 + 1), [(p["Asha"], 0.9)])
    _tick(env, _min(56))
    marks, detail = _marks(env, _sessions(env)[0].id)
    assert detail["counted_buckets"] == 2 and detail["no_coverage"] is True
    assert marks == {"Asha": "PRESENT", "Bilal": "ABSENT"}


def test_a_session_called_off_is_not_watched_or_revived_and_can_be_restored(env):  # noqa: F811
    _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    sid = _sessions(env)[0].id
    A = env.jwt("admin")
    r = env.client.post(f"{URL}/sessions/{sid}/off", headers=A, json={"reason": "Diwali"})
    assert r.status_code == 200 and r.json()["status"] == "off"
    assert env.client.post(f"{URL}/sessions/{sid}/off", headers=A).status_code == 409
    _tick(env, _min(-30))                 # the timetable still wants it
    assert _sessions(env)[0].status == "off"
    _tick(env, _min(56))
    assert _sessions(env)[0].status == "off" and _sessions(env)[0].finalized_at is None

    # Its end has passed (the test's Monday), so it goes back to be decided.
    r = env.client.post(f"{URL}/sessions/{sid}/restore", headers=A)
    assert r.status_code == 200 and r.json()["status"] == "finalizing"
    assert env.client.post(f"{URL}/sessions/{sid}/restore", headers=A).status_code == 409


def test_a_decided_session_called_off_keeps_its_marks_for_a_restore(env):  # noqa: F811
    _, p, _ = _setup(env, members=("Asha",))
    _tick(env, _min(-60))
    _tick(env, _min(0.5))
    _attempt(env, _min(1), [(p["Asha"], 0.9)])
    _tick(env, _min(56))
    sid = _sessions(env)[0].id
    A = env.jwt("admin")
    env.client.post(f"{URL}/sessions/{sid}/off", headers=A)
    assert env.client.get(f"{URL}/sessions", headers=A).json()["sessions"][0]["status"] == "off"
    r = env.client.post(f"{URL}/sessions/{sid}/restore", headers=A)
    assert r.json()["status"] == "final" and r.json()["counts"] == {"PRESENT": 1}


def test_a_one_off_session_is_added_and_runs_like_any_other(env):  # noqa: F811
    from datetime import date as _date

    g, p, _ = _setup(env, members=("Asha",))
    A = env.jwt("admin")
    future = (datetime.now(UTC) + timedelta(days=3)).date()
    r = env.client.post(f"{URL}/sessions", headers=A, json={
        "group_id": g["id"], "local_date": future.isoformat(),
        "start_time": "14:00", "end_time": "24:00", "camera_ids": [1]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slot_id"] is None and body["status"] == "scheduled"
    from zoneinfo import ZoneInfo

    midnight = datetime.combine(future + timedelta(days=1), datetime.min.time(),
                                tzinfo=ZoneInfo("Asia/Kolkata"))
    assert sched._utc(datetime.fromisoformat(body["end_at"])) == midnight.astimezone(UTC)
    # One that has already ended is refused: nobody could have watched it.
    r = env.client.post(f"{URL}/sessions", headers=A, json={
        "group_id": g["id"], "local_date": _date(2020, 1, 6).isoformat(),
        "start_time": "09:00", "end_time": "10:00", "camera_ids": [1]})
    assert r.status_code == 422
    r = env.client.post(f"{URL}/sessions", headers=A, json={
        "group_id": g["id"], "local_date": future.isoformat(),
        "start_time": "10:00", "end_time": "09:00", "camera_ids": [1]})
    assert r.status_code == 422


def test_a_slot_may_end_at_midnight(env):  # noqa: F811
    g, _, _ = _setup(env, members=("Asha",))
    r = env.client.post(f"{URL}/timetable", headers=env.jwt("admin"), json={
        "group_id": g["id"], "weekday": 1, "start_time": "18:00", "end_time": "24:00",
        "camera_ids": [1]})
    assert r.status_code == 201, r.text
    bad = env.client.post(f"{URL}/timetable", headers=env.jwt("admin"), json={
        "group_id": g["id"], "weekday": 1, "start_time": "24:00", "end_time": "24:00",
        "camera_ids": [1]})
    assert bad.status_code == 422
