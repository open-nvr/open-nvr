# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Attendance roster and timetable (routers/attendance.py).

* people, groups, members and slots are CRUD, and every change is audited;
* view / manage / enroll gate what they say, and API tokens get nothing yet;
* removing a person keeps their row, takes them out of every group and
  frees their reference number for somebody new;
* a group with history cannot be deleted, only deactivated;
* policies, timezones, times and cameras are checked before they are stored.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

URL = "/api/v1/attendance"


def _role_user(env, username, perms):  # noqa: F811
    """A non-superuser holding exactly ``perms`` and seeing camera 3 only."""
    m = env.models
    s = env.Session()
    existing = {p.name: p for p in s.query(m.Permission).all()}
    for name in perms:
        if name not in existing:
            existing[name] = m.Permission(name=name, description=name)
            s.add(existing[name])
    s.commit()
    role = m.Role(name=f"role-{username}")
    s.add(role)
    s.commit()
    for name in perms:
        s.add(m.RolePermission(role_id=role.id, permission_id=existing[name].id))
    u = m.User(username=username, email=f"{username}@x", hashed_password="h",
               is_active=True, is_superuser=False, role_id=role.id)
    s.add(u)
    s.commit()
    s.add(m.CameraPermission(user_id=u.id, camera_id=3, can_view=True))
    s.commit()
    s.close()
    return env.jwt(username)


def _person(env, name="Asha Rao", ref="R-1", **kw):  # noqa: F811
    r = env.client.post(f"{URL}/people", headers=env.jwt("admin"),
                        json={"display_name": name, "external_ref": ref, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _group(env, name="CS-A", tz="Asia/Kolkata", **kw):  # noqa: F811
    r = env.client.post(f"{URL}/groups", headers=env.jwt("admin"),
                        json={"name": name, "timezone": tz, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _slot(env, group_id, **kw):  # noqa: F811
    body = {"group_id": group_id, "weekday": 0, "start_time": "09:00",
            "end_time": "09:50", "camera_ids": [1]}
    body.update(kw)
    return env.client.post(f"{URL}/timetable", headers=env.jwt("admin"), json=body)


def _audit_actions(env):  # noqa: F811
    s = env.Session()
    rows = [a for (a,) in s.query(env.models.AuditLog.action)
            .filter(env.models.AuditLog.action.like("attendance.%"))
            .order_by(env.models.AuditLog.id).all()]
    s.close()
    return rows


# ── People ───────────────────────────────────────────────────────────


def test_person_crud_is_audited(env):  # noqa: F811
    A = env.jwt("admin")
    p = _person(env, name="  Asha Rao ", ref=" R-1 ", email="")
    assert p["display_name"] == "Asha Rao" and p["external_ref"] == "R-1"
    assert p["email"] is None and p["uuid"] and p["face_samples"] == 0

    r = env.client.patch(f"{URL}/people/{p['id']}", headers=A, json={"email": "a@school"})
    assert r.status_code == 200 and r.json()["email"] == "a@school"
    assert env.client.get(f"{URL}/people/{p['id']}", headers=A).json()["email"] == "a@school"
    assert env.client.delete(f"{URL}/people/{p['id']}", headers=A).status_code == 200
    assert env.client.get(f"{URL}/people/{p['id']}", headers=A).status_code == 404

    assert _audit_actions(env) == ["attendance.person.create", "attendance.person.update",
                                   "attendance.person.remove"]


def test_reference_numbers_are_unique_among_people_not_removed(env):  # noqa: F811
    A = env.jwt("admin")
    first = _person(env, ref="R-7")
    r = env.client.post(f"{URL}/people", headers=A,
                        json={"display_name": "Someone else", "external_ref": "R-7"})
    assert r.status_code == 409

    # Removed, the number is free again — attendly could not do this.
    env.client.delete(f"{URL}/people/{first['id']}", headers=A)
    again = _person(env, name="New student", ref="R-7")
    assert again["id"] != first["id"]

    other = _person(env, name="Third", ref="R-8")
    r = env.client.patch(f"{URL}/people/{other['id']}", headers=A, json={"external_ref": "R-7"})
    assert r.status_code == 409


def test_removed_people_keep_their_row_and_leave_their_groups(env):  # noqa: F811
    A = env.jwt("admin")
    p = _person(env)
    g = _group(env)
    env.client.post(f"{URL}/groups/{g['id']}/members", headers=A, json={"person_ids": [p["id"]]})
    env.client.delete(f"{URL}/people/{p['id']}", headers=A)

    assert env.client.get(f"{URL}/groups/{g['id']}", headers=A).json()["members"] == 0
    listed = env.client.get(f"{URL}/people", headers=A).json()
    assert listed["total"] == 0
    with_removed = env.client.get(f"{URL}/people?include_removed=true", headers=A).json()
    assert with_removed["people"][0]["removed_at"] is not None
    assert env.client.delete(f"{URL}/people/{p['id']}", headers=A).status_code == 404


def test_people_search_and_paging(env):  # noqa: F811
    A = env.jwt("admin")
    for i in range(5):
        _person(env, name=f"Student {i}", ref=f"S-{i}")
    _person(env, name="Teacher", ref="T-1", email="t@school")

    page = env.client.get(f"{URL}/people?limit=2&offset=2", headers=A).json()
    assert page["total"] == 6 and len(page["people"]) == 2
    assert env.client.get(f"{URL}/people?q=s-3", headers=A).json()["total"] == 1
    assert env.client.get(f"{URL}/people?q=SCHOOL", headers=A).json()["total"] == 1


def test_blank_name_is_refused(env):  # noqa: F811
    A = env.jwt("admin")
    assert env.client.post(f"{URL}/people", headers=A,
                           json={"display_name": "   "}).status_code == 422
    p = _person(env)
    assert env.client.patch(f"{URL}/people/{p['id']}", headers=A,
                            json={"display_name": "  "}).status_code == 422


# ── Groups and members ───────────────────────────────────────────────


def test_group_crud_and_members(env):  # noqa: F811
    A = env.jwt("admin")
    g = _group(env, code="CSA", kind_label="Section")
    a, b, c = (_person(env, name=n, ref=n) for n in ("A", "B", "C"))

    r = env.client.put(f"{URL}/groups/{g['id']}/members", headers=A,
                       json={"person_ids": [a["id"], b["id"], a["id"]]})
    assert r.status_code == 200 and r.json()["members"] == 2
    r = env.client.put(f"{URL}/groups/{g['id']}/members", headers=A,
                       json={"person_ids": [b["id"], c["id"]]})
    names = [p["display_name"] for p in env.client.get(
        f"{URL}/groups/{g['id']}/members", headers=A).json()["people"]]
    assert names == ["B", "C"]
    assert env.client.get(f"{URL}/people/{c['id']}", headers=A).json()["group_ids"] == [g["id"]]

    assert env.client.delete(f"{URL}/groups/{g['id']}/members/{b['id']}",
                             headers=A).status_code == 200
    assert env.client.delete(f"{URL}/groups/{g['id']}/members/{b['id']}",
                             headers=A).status_code == 404
    assert env.client.get(f"{URL}/groups/{g['id']}", headers=A).json()["members"] == 1

    r = env.client.patch(f"{URL}/groups/{g['id']}", headers=A, json={"name": "CS-A 2026"})
    assert r.json()["name"] == "CS-A 2026" and r.json()["code"] == "CSA"
    assert env.client.delete(f"{URL}/groups/{g['id']}", headers=A).status_code == 200


def test_unknown_people_are_refused_as_members(env):  # noqa: F811
    A = env.jwt("admin")
    g = _group(env)
    p = _person(env)
    env.client.delete(f"{URL}/people/{p['id']}", headers=A)
    r = env.client.post(f"{URL}/groups/{g['id']}/members", headers=A,
                        json={"person_ids": [p["id"], 999]})
    assert r.status_code == 422 and "999" in r.text


def test_group_codes_are_unique(env):  # noqa: F811
    _group(env, name="One", code="X1")
    r = env.client.post(f"{URL}/groups", headers=env.jwt("admin"),
                        json={"name": "Two", "code": "X1", "timezone": "UTC"})
    assert r.status_code == 409


def test_a_group_with_history_cannot_be_deleted(env):  # noqa: F811
    g = _group(env)
    s = env.Session()
    s.add(env.models.AttendanceSession(
        group_id=g["id"], local_date=date(2026, 11, 2),
        start_at=datetime(2026, 11, 2, 3, 30, tzinfo=UTC),
        end_at=datetime(2026, 11, 2, 4, 20, tzinfo=UTC), camera_ids=[1]))
    s.commit()
    s.close()
    r = env.client.delete(f"{URL}/groups/{g['id']}", headers=env.jwt("admin"))
    assert r.status_code == 409 and "deactivate" in r.text
    r = env.client.patch(f"{URL}/groups/{g['id']}", headers=env.jwt("admin"),
                         json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False


def test_timezones_must_be_iana_names(env):  # noqa: F811
    A = env.jwt("admin")
    assert env.client.post(f"{URL}/groups", headers=A,
                           json={"name": "G", "timezone": "IST"}).status_code == 422
    g = _group(env, tz="Europe/Paris")
    assert env.client.patch(f"{URL}/groups/{g['id']}", headers=A,
                            json={"timezone": "Mars/Base"}).status_code == 422


def test_an_old_zone_name_the_browser_reports_is_accepted(env):  # noqa: F811
    # Chrome names India Asia/Calcutta; Debian 13 keeps that name only in
    # tzdata-legacy, so core reads it from the tzdata package.
    assert _group(env, name="India", tz="Asia/Calcutta")["timezone"] == "Asia/Calcutta"


def test_group_policy_is_validated_against_the_site_policy(env):  # noqa: F811
    A = env.jwt("admin")
    g = _group(env, policy={"present_ratio": 0.75})
    assert g["policy"] == {"present_ratio": 0.75}

    for bad in ({"present_ratio": 2}, {"bucket_minutes": 2.5}, {"colour": "red"},
                {"match_threshold": True},
                # 300 s attempts in a 5-minute bucket: one chance, not two.
                {"attempt_interval_s": 300}):
        r = env.client.patch(f"{URL}/groups/{g['id']}", headers=A, json={"policy": bad})
        assert r.status_code == 422, bad
    ok = env.client.patch(f"{URL}/groups/{g['id']}", headers=A,
                          json={"policy": {"bucket_minutes": 10, "attempt_interval_s": 300}})
    assert ok.status_code == 200
    cleared = env.client.patch(f"{URL}/groups/{g['id']}", headers=A, json={"policy": None})
    assert cleared.json()["policy"] == {}


# ── Timetable ────────────────────────────────────────────────────────


def test_slot_crud(env):  # noqa: F811
    A = env.jwt("admin")
    g = _group(env)
    r = _slot(env, g["id"], camera_ids=[1, 2, 1], valid_from="2026-07-01",
              valid_to="2026-12-15")
    assert r.status_code == 201, r.text
    slot = r.json()
    assert slot["camera_ids"] == [1, 2] and slot["valid_to"] == "2026-12-15"

    _slot(env, g["id"], weekday=2, start_time="11:00", end_time="12:00")
    listed = env.client.get(f"{URL}/timetable?group_id={g['id']}", headers=A).json()["slots"]
    assert [(s["weekday"], s["start_time"]) for s in listed] == [(0, "09:00"), (2, "11:00")]

    body = {"group_id": g["id"], "weekday": 1, "start_time": "10:00", "end_time": "10:45",
            "camera_ids": [3]}
    r = env.client.put(f"{URL}/timetable/{slot['id']}", headers=A, json=body)
    assert r.status_code == 200 and r.json()["weekday"] == 1
    assert env.client.delete(f"{URL}/timetable/{slot['id']}", headers=A).status_code == 200
    assert env.client.put(f"{URL}/timetable/{slot['id']}", headers=A,
                          json=body).status_code == 404
    assert "attendance.slot.delete" in _audit_actions(env)


def test_slot_times_are_checked(env):  # noqa: F811
    g = _group(env)
    for kw in ({"start_time": "9:00"}, {"start_time": "24:00"},
               {"start_time": "10:00", "end_time": "09:00"},      # no overnight periods
               {"start_time": "10:00", "end_time": "10:00"},
               {"weekday": 7},
               {"valid_from": "2026-12-01", "valid_to": "2026-11-01"},
               {"camera_ids": []}):
        assert _slot(env, g["id"], **kw).status_code == 422, kw


def test_slot_cameras_must_exist_and_be_visible_to_the_editor(env):  # noqa: F811
    g = _group(env)
    assert _slot(env, g["id"], camera_ids=[1, 42]).status_code == 422

    s = env.Session()
    s.get(env.models.Camera, 2).deleted_at = datetime.now(UTC)
    s.commit()
    s.close()
    assert _slot(env, g["id"], camera_ids=[2]).status_code == 422

    # A manager who sees only camera 3 cannot point a slot at camera 1.
    M = _role_user(env, "manny", ["attendance.view", "attendance.manage"])
    body = {"group_id": g["id"], "weekday": 0, "start_time": "09:00",
            "end_time": "09:50"}
    assert env.client.post(f"{URL}/timetable", headers=M,
                           json={**body, "camera_ids": [1]}).status_code == 422
    assert env.client.post(f"{URL}/timetable", headers=M,
                           json={**body, "camera_ids": [3]}).status_code == 201


def test_deleting_a_slot_keeps_its_sessions(env):  # noqa: F811
    g = _group(env)
    slot = _slot(env, g["id"]).json()
    s = env.Session()
    s.add(env.models.AttendanceSession(
        slot_id=slot["id"], group_id=g["id"], local_date=date(2026, 11, 2),
        start_at=datetime(2026, 11, 2, 3, 30, tzinfo=UTC),
        end_at=datetime(2026, 11, 2, 4, 20, tzinfo=UTC), camera_ids=[1]))
    s.commit()
    s.close()
    assert env.client.delete(f"{URL}/timetable/{slot['id']}",
                             headers=env.jwt("admin")).status_code == 200
    s = env.Session()
    kept = s.query(env.models.AttendanceSession).one()
    assert kept.slot_id is None
    s.close()


# ── Permissions ──────────────────────────────────────────────────────


def test_view_reads_but_cannot_change(env):  # noqa: F811
    p = _person(env)
    g = _group(env)
    V = _role_user(env, "vic", ["attendance.view"])
    assert env.client.get(f"{URL}/people", headers=V).status_code == 200
    assert env.client.get(f"{URL}/groups", headers=V).status_code == 200
    assert env.client.get(f"{URL}/timetable", headers=V).status_code == 200
    assert env.client.post(f"{URL}/people", headers=V,
                           json={"display_name": "X"}).status_code == 403
    assert env.client.patch(f"{URL}/people/{p['id']}", headers=V,
                            json={"email": "x@y"}).status_code == 403
    assert env.client.post(f"{URL}/groups", headers=V,
                           json={"name": "G", "timezone": "UTC"}).status_code == 403
    assert env.client.put(f"{URL}/groups/{g['id']}/members", headers=V,
                          json={"person_ids": []}).status_code == 403


def test_manage_does_not_grant_enroll_and_enroll_does_not_grant_manage(env):  # noqa: F811
    M = _role_user(env, "manny", ["attendance.view", "attendance.manage"])
    E = _role_user(env, "erin", ["attendance.view", "attendance.enroll"])
    assert env.client.post(f"{URL}/people", headers=M,
                           json={"display_name": "X"}).status_code == 403
    assert env.client.post(f"{URL}/people", headers=E,
                           json={"display_name": "X"}).status_code == 201
    assert env.client.post(f"{URL}/groups", headers=E,
                           json={"name": "G", "timezone": "UTC"}).status_code == 403
    assert env.client.post(f"{URL}/groups", headers=M,
                           json={"name": "G", "timezone": "UTC"}).status_code == 201


def test_no_attendance_permission_sees_nothing(env):  # noqa: F811
    assert env.client.get(f"{URL}/people", headers=env.jwt("vera")).status_code == 403


def test_api_tokens_cannot_reach_the_roster(env):  # noqa: F811
    """Partner access arrives with the report (attendance.view on
    TOKEN_ROUTES); until then the roster is not a token route at all."""
    tok = _mint(env)["token"]
    r = env.client.get(f"{URL}/people", headers=_as(tok))
    assert r.status_code == 403
