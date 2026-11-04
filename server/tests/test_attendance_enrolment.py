# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Attendance enrolment (routers/attendance.py → services/attendance_faces.py).

Against a fake face gallery standing in for the attendance adapter:

* no faces without consent on record; withdrawing consent forgets them;
* a photo needs exactly one face, and one that is already somebody else
  is refused, naming who; each photo gets its own result;
* removing a person forgets their faces first, and is refused — nothing
  changed — when the face service cannot confirm it;
* everything is audited, without the images.
"""

from __future__ import annotations

import pytest

from services import attendance_faces
from tests.test_api_tokens import env  # noqa: F401 - shared fixture

URL = "/api/v1/attendance"


class FakeGallery:
    """Photos are byte strings naming who is in them: b"face:asha",
    b"faces:2", b"noface", b"face:asha,face:bilal"."""

    def __init__(self):
        self.samples: dict[str, list[bytes]] = {}
        self.who: dict[bytes, str] = {}     # photo → enrolled uuid it matches
        self.down = False
        self.calls: list[str] = []

    def _check(self):
        if self.down:
            raise attendance_faces.FaceGalleryError(503, "The attendance face service is not reachable.")

    async def identify(self, image):
        self.calls.append("identify")
        self._check()
        if image == b"noface":
            return {"faces": []}
        if image.startswith(b"faces:"):
            return {"faces": [{}] * int(image.split(b":")[1])}
        owner = self.who.get(image)
        return {"faces": [{"person_id": owner, "similarity": 0.9 if owner else 0.2}]}

    async def register(self, uuid, name, image):
        self.calls.append("register")
        self._check()
        self.samples.setdefault(uuid, []).append(image)
        self.who[image] = uuid
        return len(self.samples[uuid])

    async def forget(self, uuid):
        self.calls.append("forget")
        self._check()
        gone = self.samples.pop(uuid, None) is not None
        self.who = {k: v for k, v in self.who.items() if v != uuid}
        return gone


@pytest.fixture()
def gallery(monkeypatch):
    g = FakeGallery()
    for name in ("identify", "register", "forget"):
        monkeypatch.setattr(attendance_faces, name, getattr(g, name))
    return g


def _person(env, name="Asha", ref="R1", consent=True):  # noqa: F811
    A = env.jwt("admin")
    p = env.client.post(f"{URL}/people", headers=A,
                        json={"display_name": name, "external_ref": ref}).json()
    if consent:
        r = env.client.post(f"{URL}/people/{p['id']}/consent", headers=A, json={"given": True})
        assert r.status_code == 200 and r.json()["consent_at"]
    return p


def _enrol(env, person, *photos):  # noqa: F811
    files = [("photos", (f"p{i}.jpg", data, "image/jpeg")) for i, data in enumerate(photos)]
    return env.client.post(f"{URL}/people/{person['id']}/faces", headers=env.jwt("admin"),
                           files=files)


def test_enrolment_needs_consent(env, gallery):  # noqa: F811
    p = _person(env, consent=False)
    r = _enrol(env, p, b"a1")
    assert r.status_code == 409 and "consent" in r.text
    assert gallery.calls == []


def test_each_photo_is_checked_and_the_good_ones_kept(env, gallery):  # noqa: F811
    p = _person(env)
    r = _enrol(env, p, b"a1", b"noface", b"faces:3", b"a2", b"")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enrolled"] == 2 and body["person"]["face_samples"] == 2
    assert [x["ok"] for x in body["results"]] == [True, False, False, True, False]
    assert body["results"][1]["error"] == "no face found"
    assert "3 faces" in body["results"][2]["error"]
    assert len(gallery.samples[p["uuid"]]) == 2


def test_a_face_already_enrolled_as_someone_else_is_refused(env, gallery):  # noqa: F811
    asha = _person(env)
    bilal = _person(env, name="Bilal", ref="R2")
    _enrol(env, asha, b"asha-front")
    r = _enrol(env, bilal, b"asha-front")
    res = r.json()["results"][0]
    assert res["ok"] is False and "already enrolled as Asha" in res["error"]
    assert res["duplicate_of"] == asha["id"]
    # More of Asha's own face is fine: the match is herself.
    assert _enrol(env, asha, b"asha-front").json()["enrolled"] == 1


def test_photo_count_is_bounded(env, gallery):  # noqa: F811
    p = _person(env)
    assert _enrol(env, p, *[b"x%d" % i for i in range(11)]).status_code == 422


def test_a_down_face_service_is_503_before_anything_changes(env, gallery):  # noqa: F811
    p = _person(env)
    gallery.down = True
    r = _enrol(env, p, b"a1", b"a2")
    assert r.status_code == 503 and "not reachable" in r.text
    assert env.client.get(f"{URL}/people/{p['id']}", headers=env.jwt("admin")).json()[
        "face_samples"] == 0


def test_withdrawing_consent_forgets_the_faces(env, gallery):  # noqa: F811
    p = _person(env)
    _enrol(env, p, b"a1", b"a2")
    r = env.client.post(f"{URL}/people/{p['id']}/consent", headers=env.jwt("admin"),
                        json={"given": False})
    assert r.status_code == 200
    assert r.json()["consent_at"] is None and r.json()["face_samples"] == 0
    assert p["uuid"] not in gallery.samples
    assert _enrol(env, p, b"a3").status_code == 409


def test_removing_a_person_forgets_their_faces_first(env, gallery):  # noqa: F811
    p = _person(env)
    _enrol(env, p, b"a1")
    assert env.client.delete(f"{URL}/people/{p['id']}", headers=env.jwt("admin")).status_code == 200
    assert p["uuid"] not in gallery.samples and gallery.calls[-1] == "forget"


def test_removal_is_refused_when_faces_cannot_be_deleted(env, gallery):  # noqa: F811
    p = _person(env)
    _enrol(env, p, b"a1")
    gallery.down = True
    r = env.client.delete(f"{URL}/people/{p['id']}", headers=env.jwt("admin"))
    assert r.status_code == 503 and "nothing was changed" in r.text
    still = env.client.get(f"{URL}/people/{p['id']}", headers=env.jwt("admin")).json()
    assert still["removed_at"] is None and still["face_samples"] == 1


def test_a_person_never_enrolled_is_removed_without_the_face_service(env, gallery):  # noqa: F811
    p = _person(env, consent=False)
    gallery.down = True
    assert env.client.delete(f"{URL}/people/{p['id']}", headers=env.jwt("admin")).status_code == 200


def test_forget_faces_keeps_the_person_and_asks_the_adapter_even_at_zero(env, gallery):  # noqa: F811
    p = _person(env)
    _enrol(env, p, b"a1")
    r = env.client.delete(f"{URL}/people/{p['id']}/faces", headers=env.jwt("admin"))
    assert r.status_code == 200 and r.json()["face_samples"] == 0 and r.json()["removed_at"] is None
    gallery.calls.clear()
    env.client.delete(f"{URL}/people/{p['id']}/faces", headers=env.jwt("admin"))
    assert gallery.calls == ["forget"]


def test_enrolment_needs_the_enroll_permission_and_is_audited(env, gallery):  # noqa: F811
    from tests.test_attendance_roster import _role_user

    p = _person(env)
    M = _role_user(env, "manny", ["attendance.view", "attendance.manage"])
    r = env.client.post(f"{URL}/people/{p['id']}/faces", headers=M,
                        files=[("photos", ("a.jpg", b"a1", "image/jpeg"))])
    assert r.status_code == 403
    _enrol(env, p, b"a1", b"noface")
    s = env.Session()
    row = (s.query(env.models.AuditLog)
           .filter(env.models.AuditLog.action == "attendance.face.enrol").one())
    s.close()
    assert '"enrolled": 1' in row.details and "a1" not in row.details


# ── The HTTP client itself, against a mock adapter ──────────────────


@pytest.fixture()
def adapter(monkeypatch):
    """Route attendance_faces' httpx calls to a handler; returns the list
    of requests it saw and a slot for the handler."""
    import httpx

    seen: list[httpx.Request] = []
    box = {"handler": None}
    real = httpx.AsyncClient

    def _handler(request):
        seen.append(request)
        return box["handler"](request)

    monkeypatch.setattr(attendance_faces.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(_handler), **kw))
    return seen, box


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_client_sends_what_the_adapter_expects(adapter):
    import httpx

    from core.config import settings

    seen, box = adapter
    box["handler"] = lambda r: httpx.Response(200, json={"ok": True, "face": {"samples": 3}})
    assert _run(attendance_faces.register("u-1", "Asha", b"jpeg")) == 3
    req = seen[-1]
    assert req.url.path == "/faces/register"
    assert req.headers["authorization"] == f"Bearer {settings.internal_api_key}"
    body = req.content
    for part in (b'name="person_id"', b"u-1", b'name="append"', b"true",
                 b'name="category"', b"attendance", b'name="frame"'):
        assert part in body

    box["handler"] = lambda r: httpx.Response(200, json={"result": {"faces": [{"person_id": "u-2",
                                                                                "similarity": 0.7}]}})
    found = _run(attendance_faces.identify(b"jpeg"))
    assert b'"task": "face_identify"' in seen[-1].content and seen[-1].url.path == "/infer"
    assert attendance_faces.duplicate_of(found, "u-1") == {"person_uuid": "u-2", "similarity": 0.7}
    assert attendance_faces.duplicate_of(found, "u-2") is None


def test_client_maps_adapter_errors(adapter):
    import httpx

    _seen, box = adapter
    box["handler"] = lambda r: httpx.Response(404, json={"detail": "no registered face"})
    assert _run(attendance_faces.forget("u-1")) is False

    box["handler"] = lambda r: httpx.Response(422, json={"detail": "image contains 2 faces"})
    with pytest.raises(attendance_faces.FaceGalleryError) as e:
        _run(attendance_faces.register("u-1", "A", b"x"))
    assert e.value.status == 422 and "2 faces" in e.value.message

    # An adapter image from before face_identify answers without faces[].
    box["handler"] = lambda r: httpx.Response(200, json={"result": {"task": "face_detection"}})
    with pytest.raises(attendance_faces.FaceGalleryError) as e:
        _run(attendance_faces.identify(b"x"))
    assert e.value.status == 502 and "face_identify" in e.value.message

    def _refuse(_r):
        raise httpx.ConnectError("refused")

    box["handler"] = _refuse
    with pytest.raises(attendance_faces.FaceGalleryError) as e:
        _run(attendance_faces.forget("u-1"))
    assert e.value.status == 503


def test_removal_asks_the_adapter_even_when_the_count_says_zero(env, gallery):  # noqa: F811
    """The count is written after an enrolment's last photo; a crash in
    between leaves faces it never saw."""
    p = _person(env)
    gallery.samples[p["uuid"]] = [b"orphan"]          # in the adapter, not in core's count
    assert env.client.delete(f"{URL}/people/{p['id']}", headers=env.jwt("admin")).status_code == 200
    assert p["uuid"] not in gallery.samples


def test_consent_withdrawn_mid_enrolment_keeps_nothing(env, gallery, monkeypatch):  # noqa: F811
    p = _person(env)
    real_register = gallery.register

    async def register_then_withdraw(uuid, name, image):
        n = await real_register(uuid, name, image)
        # The person's consent is withdrawn while the next photo is in flight.
        s = env.Session()
        row = s.get(env.models.AttendancePerson, p["id"])
        row.consent_at = None
        s.commit()
        s.close()
        return n

    monkeypatch.setattr(attendance_faces, "register", register_then_withdraw)
    r = _enrol(env, p, b"a1", b"a2", b"a3")
    assert r.status_code == 409 and "nothing from it was kept" in r.text
    assert p["uuid"] not in gallery.samples
