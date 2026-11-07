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

    async def identify(self, image, match=True):
        self.calls.append("identify")
        self._check()
        if image == b"noface":
            return {"faces": []}
        if image.startswith(b"faces:"):
            return {"faces": [{}] * int(image.split(b":")[1])}
        owner = self.who.get(image)
        return {"faces": [{"person_id": owner, "similarity": 0.9 if owner else 0.2}]}

    async def register(self, uuid, image):
        self.calls.append("register")
        self._check()
        self.samples.setdefault(uuid, []).append(image)
        self.who[image] = uuid
        return len(self.samples[uuid])

    async def forget_sample(self, uuid, index):
        self.calls.append("forget_sample")
        self._check()
        samples = self.samples.get(uuid) or []
        if not 0 <= index < len(samples):
            raise attendance_faces.FaceGalleryError(404, "That photo is no longer enrolled.")
        if len(samples) == 1:
            raise attendance_faces.FaceGalleryError(409, "This is their only enrolled photo.")
        self.who.pop(samples.pop(index), None)
        return len(samples)

    async def forget(self, uuid):
        self.calls.append("forget")
        self._check()
        gone = self.samples.pop(uuid, None) is not None
        self.who = {k: v for k, v in self.who.items() if v != uuid}
        return gone


@pytest.fixture()
def gallery(monkeypatch):
    g = FakeGallery()
    for name in ("identify", "register", "forget", "forget_sample"):
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


def test_a_photo_can_be_checked_for_a_duplicate_before_enrolling(env, gallery):  # noqa: F811
    asha = _person(env)
    bilal = _person(env, name="Bilal", ref="R2")
    _enrol(env, asha, b"asha-front")

    def check(person, data):
        return env.client.post(f"{URL}/people/{person['id']}/faces/duplicate", headers=env.jwt("admin"),
                               files={"photo": ("p.jpg", data, "image/jpeg")})

    r = check(bilal, b"asha-front")
    assert r.status_code == 200, r.text
    dup = r.json()["duplicate"]
    assert dup["id"] == asha["id"] and dup["display_name"] == "Asha" and dup["external_ref"] == "R1"
    # Her own face, a stranger's and a photo with no face: no duplicate.
    assert check(asha, b"asha-front").json() == {"duplicate": None}
    assert check(bilal, b"stranger").json() == {"duplicate": None}
    assert check(bilal, b"noface").json() == {"duplicate": None}
    # Nothing was enrolled by asking.
    assert bilal["uuid"] not in gallery.samples
    assert check(bilal, b"").status_code == 422
    # Who is enrolled is not for a viewer.
    r = env.client.post(f"{URL}/people/{bilal['id']}/faces/duplicate", headers=env.jwt("vera"),
                        files={"photo": ("p.jpg", b"asha-front", "image/jpeg")})
    assert r.status_code == 403


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
    assert _run(attendance_faces.register("u-1", b"jpeg")) == 3
    req = seen[-1]
    assert req.url.path == "/faces/register"
    assert req.headers["authorization"] == f"Bearer {settings.internal_api_key}"
    body = req.content
    for part in (b'name="person_id"', b"u-1", b'name="append"', b"true",
                 b'name="category"', b"attendance", b'name="frame"'):
        assert part in body
    # The adapter's "name" is the uuid: KAI-C would hand a real name to
    # any holder of the site key who asks it to identify a frame.
    assert b'name="name"\r\n\r\nu-1\r\n' in body

    box["handler"] = lambda r: httpx.Response(200, json={"result": {"faces": [{"person_id": "u-2",
                                                                                "similarity": 0.7}]}})
    found = _run(attendance_faces.identify(b"jpeg"))
    assert b'"task": "face_identify"' in seen[-1].content and seen[-1].url.path == "/infer"
    assert attendance_faces.duplicate_of(found, "u-1") == {"person_uuid": "u-2", "similarity": 0.7}
    assert attendance_faces.duplicate_of(found, "u-2") is None
    _run(attendance_faces.identify(b"jpeg", match=False))
    assert b'"match": false' in seen[-1].content


def test_client_maps_adapter_errors(adapter):
    import httpx

    _seen, box = adapter
    box["handler"] = lambda r: httpx.Response(404, json={"detail": "no registered face"})
    assert _run(attendance_faces.forget("u-1")) is False

    box["handler"] = lambda r: httpx.Response(422, json={"detail": "image contains 2 faces"})
    with pytest.raises(attendance_faces.FaceGalleryError) as e:
        _run(attendance_faces.register("u-1", b"x"))
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

    async def register_then_withdraw(uuid, image):
        n = await real_register(uuid, image)
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


@pytest.mark.parametrize("what", ["consent", "removed"])
def test_a_withdrawal_during_the_last_photo_keeps_nothing(env, gallery, monkeypatch, what):  # noqa: F811
    """The check before each photo cannot see a withdrawal that lands
    while the LAST one is with the adapter; forget has already run by
    then, so the faces would outlive it (and, for a removed person, no
    route could reach them)."""
    p = _person(env)
    real_register = gallery.register

    async def register_then_withdraw(uuid, image):
        n = await real_register(uuid, image)
        s = env.Session()
        row = s.get(env.models.AttendancePerson, p["id"])
        if what == "consent":
            row.consent_at = None
        else:
            from datetime import UTC, datetime
            row.deleted_at = datetime.now(UTC)
        s.commit()
        s.close()
        return n

    monkeypatch.setattr(attendance_faces, "register", register_then_withdraw)
    r = _enrol(env, p, b"only")
    assert r.status_code == 409 and "nothing from it was kept" in r.text
    assert p["uuid"] not in gallery.samples
    s = env.Session()
    assert s.get(env.models.AttendancePerson, p["id"]).face_samples == 0
    s.close()


# ── The person's picture, and the webcam guidance check ─────────────

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


@pytest.fixture(autouse=True)
def photos_dir(monkeypatch, tmp_path):
    from core.config import settings

    monkeypatch.setattr(settings, "recordings_base_path", str(tmp_path))
    return tmp_path


def _put_photo(env, person, data=JPEG, headers=None):  # noqa: F811
    return env.client.put(f"{URL}/people/{person['id']}/photo", content=data,
                          headers={**(headers or env.jwt("admin")), "content-type": "image/jpeg"})


def test_a_picture_is_kept_shown_and_audited(env, gallery, photos_dir):  # noqa: F811
    p = _person(env)
    assert p["has_photo"] is False
    assert env.client.get(f"{URL}/people/{p['id']}/photo", headers=env.jwt("admin")).status_code == 404
    r = _put_photo(env, p)
    assert r.status_code == 200 and r.json()["has_photo"] is True
    got = env.client.get(f"{URL}/people/{p['id']}/photo", headers=env.jwt("admin"))
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers["content-type"] == "image/jpeg"
    # Outside the folders the retention sweep empties.
    assert (photos_dir / ".attendance" / "people" / f"{p['uuid']}.jpg").is_file()
    assert not (photos_dir / ".evidence").exists()
    s = env.Session()
    assert s.query(env.models.AuditLog).filter(
        env.models.AuditLog.action == "attendance.photo.set").count() == 1
    s.close()


def test_a_picture_needs_consent_a_jpeg_and_a_small_one(env, gallery, photos_dir):  # noqa: F811
    assert _put_photo(env, _person(env, consent=False)).status_code == 409
    p = _person(env, name="Bilal", ref="R2")
    assert _put_photo(env, p, b"GIF89a").status_code == 422
    assert _put_photo(env, p, b"").status_code == 422
    big = JPEG + b"0" * attendance_faces.MAX_PHOTO_BYTES
    assert _put_photo(env, p, big).status_code == 413


def test_a_picture_needs_the_enroll_permission(env, gallery, photos_dir):  # noqa: F811
    from tests.test_attendance_roster import _role_user

    p = _person(env)
    V = _role_user(env, "picviewer", ["attendance.view"])
    assert _put_photo(env, p, headers=V).status_code == 403
    _put_photo(env, p)
    assert env.client.get(f"{URL}/people/{p['id']}/photo", headers=V).status_code == 200


@pytest.mark.parametrize("how", ["withdraw", "forget", "remove"])
def test_the_picture_goes_with_the_faces(env, gallery, photos_dir, how):  # noqa: F811
    p = _person(env)
    _enrol(env, p, b"a1")
    _put_photo(env, p)
    A = env.jwt("admin")
    if how == "withdraw":
        env.client.post(f"{URL}/people/{p['id']}/consent", headers=A, json={"given": False})
    elif how == "forget":
        env.client.delete(f"{URL}/people/{p['id']}/faces", headers=A)
    else:
        env.client.delete(f"{URL}/people/{p['id']}", headers=A)
    assert not attendance_faces.has_photo(p["uuid"])


def test_an_accepted_photo_says_where_the_face_is(env, gallery, monkeypatch):  # noqa: F811
    async def identify(image, match=True):
        return {"faces": [{"bbox": [10, 20, 110, 140], "person_id": None, "similarity": 0.1}]}

    monkeypatch.setattr(attendance_faces, "identify", identify)
    r = _enrol(env, _person(env), b"a1")
    assert r.json()["results"][0]["bbox"] == [10, 20, 110, 140]


def test_the_guidance_check_gives_boxes_and_never_names(env, gallery, monkeypatch):  # noqa: F811
    from tests.test_attendance_roster import _role_user

    asked = []

    async def identify(image, match=True):
        asked.append(match)
        return {"faces": [{"bbox": [1, 2, 3, 4], "confidence": 0.9, "person_id": "u-1",
                           "name": "Asha", "similarity": 0.8, "landmarks": [[1, 1], [2, 1]]}],
                "dropped": {"too_small": 2}, "frame": {"w": 640, "h": 360}}

    monkeypatch.setattr(attendance_faces, "identify", identify)
    frame = [("frame", ("f.jpg", b"jpeg", "image/jpeg"))]
    r = env.client.post(f"{URL}/faces/check", headers=env.jwt("admin"), files=frame)
    assert r.status_code == 200
    assert r.json() == {"faces": [{"bbox": [1, 2, 3, 4], "confidence": 0.9,
                                   "landmarks": [[1, 1], [2, 1]]}],
                        "too_small": 2, "frame": {"w": 640, "h": 360}}
    assert "Asha" not in r.text and "u-1" not in r.text
    assert asked == [False], "the guidance check asks for the detector alone"
    V = _role_user(env, "vic", ["attendance.view"])
    assert env.client.post(f"{URL}/faces/check", headers=V, files=frame).status_code == 403
    gallery.down = True
    monkeypatch.setattr(attendance_faces, "identify", gallery.identify)
    assert env.client.post(f"{URL}/faces/check", headers=env.jwt("admin"),
                           files=frame).status_code == 503


# ── One set is one person; the enrolled photos are kept and shown ────

J = b"\xff\xd8"   # a photo that is a JPEG, so it is kept


def test_a_set_with_one_photo_of_someone_else_enrols_none(env, gallery):  # noqa: F811
    asha = _person(env)
    bilal = _person(env, name="Bilal", ref="R2")
    _enrol(env, asha, J + b"asha-front")
    # Asha again, under Bilal: her turned head is under the duplicate
    # threshold, her straight-on photo is not.
    r = _enrol(env, bilal, J + b"asha-left", J + b"asha-front", b"noface")
    body = r.json()
    assert body["enrolled"] == 0 and body["person"]["face_samples"] == 0
    assert bilal["uuid"] not in gallery.samples
    assert [x["ok"] for x in body["results"]] == [False, False, False]
    assert "photo 2 of this set is already enrolled as Asha" in body["results"][0]["error"]
    assert body["results"][0]["duplicate_of"] == asha["id"]
    assert "already enrolled as Asha" in body["results"][1]["error"]
    assert body["results"][2]["error"] == "no face found"
    assert "register" not in gallery.calls[-3:]


def test_the_enrolled_photos_are_listed_and_shown(env, gallery):  # noqa: F811
    p = _person(env)
    _enrol(env, p, b"not-a-jpeg")            # enrolled, but nothing to show
    _enrol(env, p, J + b"a1", J + b"a2")
    A = env.jwt("admin")
    listed = env.client.get(f"{URL}/people/{p['id']}/faces", headers=A).json()
    assert listed["samples"] == 3
    assert [x["kept"] for x in listed["photos"]] == [False, True, True]
    assert listed["photos"][1]["enrolled_at"]
    img = env.client.get(f"{URL}/people/{p['id']}/faces/2/image", headers=A)
    assert img.status_code == 200 and img.content == J + b"a2"
    assert env.client.get(f"{URL}/people/{p['id']}/faces/0/image", headers=A).status_code == 404


def test_samples_enrolled_before_photos_were_kept_are_gaps_in_front(env, gallery):  # noqa: F811
    p = _person(env)
    gallery.samples[p["uuid"]] = [b"old1", b"old2"]   # enrolled by an older core
    _enrol(env, p, J + b"new")
    listed = env.client.get(f"{URL}/people/{p['id']}/faces", headers=env.jwt("admin")).json()
    assert [x["kept"] for x in listed["photos"]] == [False, False, True]


def test_one_photo_can_be_deleted_and_the_rest_keep_their_place(env, gallery):  # noqa: F811
    p = _person(env)
    _enrol(env, p, J + b"a1", J + b"a2", J + b"a3")
    A = env.jwt("admin")
    r = env.client.delete(f"{URL}/people/{p['id']}/faces/1", headers=A)
    assert r.status_code == 200 and r.json()["face_samples"] == 2
    assert gallery.samples[p["uuid"]] == [J + b"a1", J + b"a3"]
    assert env.client.get(f"{URL}/people/{p['id']}/faces/1/image", headers=A).content == J + b"a3"
    s = env.Session()
    assert s.query(env.models.AuditLog).filter(
        env.models.AuditLog.action == "attendance.face.forget_one").count() == 1
    s.close()
    env.client.delete(f"{URL}/people/{p['id']}/faces/0", headers=A)
    last = env.client.delete(f"{URL}/people/{p['id']}/faces/0", headers=A)
    assert last.status_code == 409 and "only enrolled photo" in last.text
    assert env.client.delete(f"{URL}/people/{p['id']}/faces/5", headers=A).status_code == 404


def test_the_enrolled_photos_go_with_the_faces(env, gallery, photos_dir):  # noqa: F811
    p = _person(env)
    _enrol(env, p, J + b"a1")
    folder = photos_dir / ".attendance" / "people" / p["uuid"]
    assert any(folder.glob("*.jpg"))
    env.client.delete(f"{URL}/people/{p['id']}/faces", headers=env.jwt("admin"))
    assert not folder.exists()


def test_enrolled_photos_need_the_enroll_permission(env, gallery):  # noqa: F811
    from tests.test_attendance_roster import _role_user

    p = _person(env)
    _enrol(env, p, J + b"a1")
    V = _role_user(env, "photoviewer", ["attendance.view"])
    assert env.client.get(f"{URL}/people/{p['id']}/faces", headers=V).status_code == 403
    assert env.client.get(f"{URL}/people/{p['id']}/faces/0/image", headers=V).status_code == 403
    assert env.client.delete(f"{URL}/people/{p['id']}/faces/0", headers=V).status_code == 403


def test_erasure_replaces_who_they_were_and_reaches_the_already_removed(env, gallery):  # noqa: F811
    A = env.jwt("admin")
    p = _person(env, name="Asha Rao", ref="R-77")
    r = env.client.delete(f"{URL}/people/{p['id']}?erase=true", headers=A)
    assert r.status_code == 200 and r.json() == {"removed": p["id"], "erased": True}
    s = env.Session()
    row = s.get(env.models.AttendancePerson, p["id"])
    assert (row.display_name, row.external_ref, row.email) == (f"Erased person #{p['id']}", None, None)
    assert row.deleted_at is not None and p["uuid"] not in gallery.samples
    s.close()

    q = _person(env, name="Bilal Khan", ref="R-78")
    assert env.client.delete(f"{URL}/people/{q['id']}", headers=A).status_code == 200
    assert env.client.delete(f"{URL}/people/{q['id']}", headers=A).status_code == 404
    assert env.client.delete(f"{URL}/people/{q['id']}?erase=true", headers=A).status_code == 200
    s = env.Session()
    assert s.get(env.models.AttendancePerson, q["id"]).display_name == f"Erased person #{q['id']}"
    s.close()
