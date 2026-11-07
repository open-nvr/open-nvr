"""Enrolment, taken from PR #791: consent first, the webcam's frame check
and per-photo duplicate check, the picture, withdrawal and erasure."""
import io

import cv2
import numpy as np

from attendly.storage import repo
from tests.conftest import jpeg_bytes


def _up(name, data):
    return (name, io.BytesIO(data), "image/jpeg")


def test_consent_is_required_before_faces_are_enrolled(client, env):
    r = client.post("/students/", json={"full_name": "Asha", "course_code": "MBA-1", "section": "A", "email": "a@example.com"})
    code = r.json()["student_code"]
    assert r.json()["consent_at"] is None
    r = client.post(f"/students/{code}/register", files=[("images", _up("a.jpg", jpeg_bytes(seed=1)))])
    assert r.status_code == 403 and "onsent" in r.json()["detail"]
    r = client.post("/students/register_new", data={"full_name": "B", "course_code": "MBA-1", "section": "A", "email": "b@example.com"},
                    files=[("images", _up("a.jpg", jpeg_bytes(seed=2)))])
    assert r.status_code == 403
    r = client.post(f"/students/{code}/consent")
    assert r.status_code == 200 and r.json()["consent_at"]
    r = client.post(f"/students/{code}/register", files=[("images", _up("a.jpg", jpeg_bytes(seed=1)))])
    assert r.status_code == 200 and r.json()["num_faces_registered"] == 1
    # register_new with consent records it
    r = client.post("/students/register_new", data={"consent": "true", "full_name": "B", "course_code": "MBA-1", "section": "A",
                                                     "email": "b@example.com"}, files=[("images", _up("a.jpg", jpeg_bytes(seed=2)))])
    assert r.status_code == 200 and r.json()["student"]["consent_at"]


def test_withdrawal_deletes_faces_and_picture(client, env):
    eng = env["engine"]
    r = client.post("/students/register_new", data={"consent": "true", "full_name": "Asha", "course_code": "MBA-1", "section": "A",
                                                     "email": "a@example.com"}, files=[("images", _up("a.jpg", jpeg_bytes(seed=1)))])
    code = r.json()["student"]["student_code"]
    pic = jpeg_bytes(width=64, height=64, seed=7)
    assert client.put(f"/students/{code}/picture", files={"image": _up("p.jpg", pic)}).json()["ok"] is True
    r = client.get(f"/students/{code}/picture")
    assert r.status_code == 200 and r.content == pic
    assert client.get("/students/").json()[0]["has_picture"] is True
    assert client.put(f"/students/{code}/picture", files={"image": _up("p.txt", b"not an image")}).status_code == 400
    r = client.post(f"/students/{code}/consent/withdraw")
    assert r.status_code == 200 and r.json()["consent_at"] is None and r.json()["has_picture"] is False
    assert code not in eng.enrolled and client.get(f"/students/{code}/picture").status_code == 404
    # the record is still there, and consent can be recorded again
    assert client.get("/students/").json()[0]["student_code"] == code
    assert client.post(f"/students/{code}/consent").json()["consent_at"]


def test_erase_blanks_the_person_and_keeps_the_counts(client, env):
    from datetime import datetime, timezone

    r = client.post("/students/register_new", data={"consent": "true", "full_name": "Asha Rao", "course_code": "MBA-1", "section": "A",
                                                     "email": "asha@example.com", "residence": "hostler", "hostel_block": "B"},
                    files=[("images", _up("a.jpg", jpeg_bytes(seed=1)))])
    code = r.json()["student"]["student_code"]
    client.put(f"/students/{code}/enrolments", json=[{"course_code": "ECO-2", "section": "B"}])
    class_id = "MBA-1-A-20260105-0900-1000"
    repo.save_snapshot(class_id, [code], datetime(2026, 1, 5, 9, 10, tzinfo=timezone.utc))
    repo.finalize_attendance(class_id)
    # a plain delete retires the row and deletes the faces
    r = client.delete(f"/students/{code}")
    assert r.json() == {"ok": True, "student_code": code, "faces_deleted": 1, "erased": False}
    assert client.delete(f"/students/{code}").status_code == 404  # already removed
    # erase works on a removed student too, and blanks who they were
    r = client.delete(f"/students/{code}", params={"erase": "true"})
    assert r.status_code == 200 and r.json()["erased"] is True
    row = repo.get_students_batch([code], include_deleted=True)[code]
    assert row["full_name"] == "Erased" and row["email"] is None and row["hostel_block"] is None and row["erased_at"]
    assert repo.list_student_enrolments(code) == []
    marks = client.get(f"/attendance/{class_id}").json()
    assert marks[0]["student"]["full_name"] == "Erased" and marks[0]["status"] == "PRESENT"  # the count stays
    assert client.get("/students/").json() == []


def test_check_frame_and_check_duplicate(client, env):
    eng = env["engine"]
    photo = jpeg_bytes(width=320, height=240, seed=3)
    # detect-then-crop adapter: boxes only, small faces reported as too small
    eng.faces_in[photo] = [[40, 40, 140, 160]]
    r = client.post("/students/check_frame", files={"image": _up("f.jpg", photo)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["frame"] == {"w": 320, "h": 240} and body["faces"][0]["bbox"] == [40, 40, 140, 160]
    assert body["faces"][0]["landmarks"] is None and body["too_small"] is False
    eng.faces_in[photo] = [[40, 40, 50, 50]]
    body = client.post("/students/check_frame", files={"image": _up("f.jpg", photo)}).json()
    assert body["faces"] == [] and body["too_small"] is True
    # face_identify adapter: landmarks come through, nobody is looked up
    eng.identify = {"faces": [{"bbox": [40, 40, 140, 160], "confidence": 0.9, "landmarks": [[60, 80], [120, 80], [90, 110], [70, 135], [110, 135]]}],
                    "dropped": {"too_small": 0, "over_limit": 0}}
    body = client.post("/students/check_frame", files={"image": _up("f.jpg", photo)}).json()
    assert body["faces"][0]["landmarks"][2] == [90, 110] and eng.identify_calls[-1]["match"] is False
    assert client.post("/students/check_frame", files={"image": _up("f.txt", b"nope")}).status_code == 400
    # the per-photo duplicate check names the person already enrolled
    repo.put_student("AT00001", "Asha Rao", None, "asha@example.com", course_code="MBA-1", section="A")
    from attendly.routers import students as students_router

    students_router.check_for_duplicate_face_sync = lambda img, cur: {"student_code": "AT00001", "full_name": "Asha Rao", "email": "asha@example.com",
                                                                        "kind": "student", "similarity": 91.0} if cur != "AT00001" else None
    try:
        body = client.post("/students/check_duplicate", files={"image": _up("f.jpg", photo)}).json()
        assert body["duplicate"]["student_code"] == "AT00001"
        body = client.post("/students/check_duplicate", data={"exclude_code": "AT00001"}, files={"image": _up("f.jpg", photo)}).json()
        assert body["duplicate"] is None
    finally:
        del students_router.check_for_duplicate_face_sync
        import importlib

        importlib.reload(students_router)


def test_video_registration_refuses_without_consent(client, env):
    r = client.post("/video_registration/start_video_registration", data={"full_name": "A", "course_code": "C", "section": "A", "email": "a@example.com"})
    sid = r.json()["session_id"]
    with client.websocket_connect(f"/video_registration/ws/{sid}") as ws:
        ws.send_json({"type": "init", "full_name": "A", "course_code": "C", "section": "A", "email": "a@example.com"})
        msg = ws.receive_json()
        assert msg["type"] == "error" and "onsent" in msg["message"]
