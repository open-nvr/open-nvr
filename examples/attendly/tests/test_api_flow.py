"""The application through its API with the fakes: rooms from the camera
roster, enrolment, a class, a snapshot, finalisation, the report."""
import io
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np

from attendly.storage import repo
from tests.conftest import jpeg_bytes


def _upload(name: str, data: bytes):
    return (name, io.BytesIO(data), "image/jpeg")


def test_cameras_and_rooms(client, env):
    cams = client.get("/cameras/").json()
    assert [c["name"] for c in cams] == ["Lab 1 camera", "Hall camera"]
    r = client.post("/classrooms/", json={"name": "Lab 1", "camera_id": 7})
    assert r.status_code == 200
    room = r.json()
    assert room["camera_id"] == 7 and room["camera_name"] == "Lab 1 camera"
    assert client.post("/classrooms/", json={"name": "No cam"}).status_code == 400
    assert client.post("/classrooms/", json={"name": "Bad", "camera_id": "x"}).status_code == 400
    r = client.put(f"/classrooms/{room['id']}", json={"camera_id": 9})
    assert r.json()["camera_name"] == "Hall camera"
    assert client.get("/streams/").json()[0]["camera_id"] == 9
    assert client.delete(f"/classrooms/{room['id']}").json() == {"ok": True}
    assert client.get("/classrooms/").json() == []


def test_cameras_unreachable_is_502(client, env):
    class Down:
        def roster(self):
            return None

    from attendly.cameras import CameraSource

    CameraSource.set_shared(CameraSource(Down()))
    r = client.get("/cameras/")
    assert r.status_code == 502 and "OpenNVR" in r.json()["detail"]
    # a room can still be created by id; the camera name is just unknown
    assert client.post("/classrooms/", json={"name": "Lab 1", "camera_id": 7}).json()["camera_name"] is None


def test_register_new_student_enrols_then_persists(client, env):
    eng = env["engine"]
    good1, good2, bad = jpeg_bytes(seed=1), jpeg_bytes(seed=2), jpeg_bytes(seed=3)
    eng.reject_calls.add(3)
    r = client.post("/students/register_new", data={"full_name": "Asha Rao", "course_code": "MBA-1", "section": "A",
                                                     "email": "asha@example.com", "angle_tags": ["front", "left", "right"]},
                    files=[("images", _upload("a.jpg", good1)), ("images", _upload("b.jpg", good2)), ("images", _upload("c.jpg", bad))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["student"]["student_code"] == "AT00001" and body["student"]["course_code"] == "MBA-1"
    assert body["num_faces_registered"] == 2 and body["angles"] == ["front", "left"]
    assert body["validation_errors"] and "Image 3" in body["validation_errors"][0]
    assert eng.enrolled["AT00001"]["samples"] == 2 and eng.enrolled["AT00001"]["name"] == ""  # no name reaches the adapter
    # the course was auto-created; the email is taken
    assert client.get("/courses/MBA-1/sections").json()["sections"] == ["A"]
    r = client.post("/students/register_new", data={"full_name": "Dup", "course_code": "MBA-1", "section": "A", "email": "asha@example.com"},
                    files=[("images", _upload("a.jpg", jpeg_bytes(seed=4)))])
    assert r.status_code == 409


def test_register_new_rejects_a_duplicate_face_and_all_bad_images(client, env):
    eng = env["engine"]
    repo.put_student("AT00009", "Existing", None, None, course_code="MBA-1", section="A")
    dup = jpeg_bytes(seed=5)
    # the duplicate check runs on the enhanced image, so match any image
    eng.search_faces = lambda image_bytes, threshold=None, max_faces=1: [("AT00009", 0.9)]
    r = client.post("/students/register_new", data={"full_name": "New", "course_code": "MBA-1", "section": "A", "email": "n@example.com"},
                    files=[("images", _upload("a.jpg", dup))])
    assert r.status_code == 409 and "Duplicate face" in r.json()["detail"]["errors"][0]
    eng.search_faces = lambda image_bytes, threshold=None, max_faces=1: []
    eng.index_face = lambda *a, **k: (_ for _ in ()).throw(__import__("attendly.face.engine", fromlist=["FaceRejected"]).FaceRejected("no face detected"))
    r = client.post("/students/register_new", data={"full_name": "New", "course_code": "MBA-1", "section": "A", "email": "n@example.com"},
                    files=[("images", _upload("a.jpg", jpeg_bytes(seed=6)))])
    assert r.status_code == 400 and "no face detected" in r.json()["detail"]["errors"][0]
    assert repo.count_students() == 1  # nothing persisted


def test_register_more_faces_for_existing_student(client, env):
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    r = client.post("/students/AT00001/register", files=[("images", _upload("a.jpg", jpeg_bytes(seed=7)))])
    assert r.status_code == 200 and r.json()["num_faces_registered"] == 1
    assert env["engine"].enrolled["AT00001"]["name"] == ""
    assert client.post("/students/NOPE/register", files=[("images", _upload("a.jpg", jpeg_bytes()))]).status_code == 404
    v = client.get("/video_registration/verify_registration/AT00001").json()
    assert v["registered"] is True and v["face_count"] == 1


def test_registry_stats_and_soft_delete(client, env):
    eng = env["engine"]
    for code, name in (("AT00001", "Asha"), ("AT00002", "Bilal")):
        repo.put_student(code, name, None, f"{code}@x.io", course_code="MBA-1", section="A")
        eng.index_face(jpeg_bytes(), code, name=name)
    eng.index_face(jpeg_bytes(), "AT00001", name="Asha")
    eng.index_face(jpeg_bytes(), "ghost", name="Ghost")  # enrolled but no student row
    s = client.get("/registry/stats", params={"include_students": "true"}).json()
    assert s["statistics"]["total_students_with_faces"] == 2 and s["statistics"]["total_faces"] == 3
    assert s["statistics"]["students_by_face_count"] == {"2": 1, "1": 1}
    rows = {r["student_code"]: r for r in s["students_with_faces"]}
    assert rows["AT00001"]["face_count"] == 2 and rows["AT00001"]["student_email"] == "AT00001@x.io"
    assert client.get("/registry/dashboard").json()["collection"]["total_faces"] == 4
    assert client.get("/registry/student/AT00001/faces").json()["total_faces"] == 2
    r = client.post("/registry/student/AT00001/soft_delete").json()
    assert r["deleted_faces"] == 2 and "AT00001" not in eng.enrolled
    assert client.get("/students/").json()[0]["student_code"] == "AT00002"
    assert client.post("/registry/student/AT00001/soft_delete").status_code == 404
    assert client.get("/registry/engine_status").json() == {"ok": True, "workers": []}


def test_class_snapshot_finalise_and_report(client, env):
    eng, cams = env["engine"], env["cameras"]
    for code, name in (("AT00001", "Asha"), ("AT00002", "Bilal")):
        repo.put_student(code, name, None, None, course_code="MBA-1", section="A")
    room = client.post("/classrooms/", json={"name": "Lab 1", "camera_id": 7}).json()
    # a frame with two faces; the first crop is Asha, the second nobody
    frame = np.full((200, 300, 3), 128, dtype=np.uint8)
    cams.frames[7] = frame
    full = cv2.imencode(".jpg", frame)[1].tobytes()
    eng.faces_in[full] = [[20, 20, 80, 90], [150, 30, 220, 110]]

    def search(image_bytes, threshold=None, max_faces=1):
        arr = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        return [("AT00001", 0.8)] if arr.shape[1] < 100 else []

    eng.search_faces = search
    class_id = "MBA-1-A-20260105-0900-1000"
    r = client.post(f"/streams/{room['id']}/snapshot", params={"class_id": class_id}).json()
    assert r["ok"] is True and r["recognized"] == ["AT00001"]
    assert client.post(f"/streams/{room['id']}/probe").json()["matches"][0]["student_code"] == "AT00001"
    r = client.post(f"/classes/{class_id}/finalize").json()
    assert r["written"] == 2 and r["total_slots"] == 1
    rows = client.get(f"/attendance/{class_id}").json()
    assert {(x["student"]["student_code"], x["status"]) for x in rows} == {("AT00001", "PRESENT"), ("AT00002", "ABSENT")}
    assert client.get("/attendance/classes").json() == [class_id]
    assert len(client.get("/attendance/by-course/MBA-1").json()) == 2
    assert client.get("/attendance/by-course/CS").json() == []
    # a room without a frame
    cams.frames[7] = None
    assert client.post(f"/streams/{room['id']}/snapshot", params={"class_id": class_id}).json()["error"] == "no_frame"


def test_schedules_and_planner_publish(client, env):
    room = client.post("/classrooms/", json={"name": "Lab 1", "camera_id": 7}).json()
    r = client.post("/schedules/", json={"room_id": room["id"], "course_code": "MBA-1", "section": "A",
                                          "start_time": "09:00", "end_time": "10:00", "date": "2026-01-05"})
    assert r.status_code == 200 and r.json()["class_id"] == "MBA-1-A-20260105-0900-1000"
    cfg = client.get(f"/streams/{room['id']}/config").json()
    assert cfg["schedule"][0]["course"] == "MBA-1" and cfg["schedule"][0]["start_iso"].startswith("2026-01-05T09:00:00")
    assert client.post("/schedules/", json={"room_id": room["id"], "course_code": "MBA-1", "section": "A",
                                             "start_time": "10:00", "end_time": "09:00", "date": "2026-01-05"}).status_code == 400
    # weekly template → publish for a Monday-to-Tuesday window: one class
    t = client.post("/planner/schedules", json={"day_of_week": "Monday", "course": "MBA-1", "section": "A",
                                                 "room_id": room["id"], "start": "11:00", "end": "12:00"}).json()
    assert client.get("/planner/schedules", params={"day": "Monday"}).json()[0]["id"] == t["id"]
    r = client.post("/planner/publish", json={"from_date": "2026-01-05", "to_date": "2026-01-06"}).json()
    assert r == {"ok": True, "scheduled": 1}
    # publishing again adds nothing
    assert client.post("/planner/publish", json={"from_date": "2026-01-05", "to_date": "2026-01-06"}).json()["scheduled"] == 0
    ids = [e["class_id"] for e in client.get(f"/streams/{room['id']}/config").json()["schedule"]]
    assert ids == ["MBA-1-A-20260105-0900-1000", "MBA-1-A-20260105-1100-1200"]
    # room courses
    client.post(f"/streams/{room['id']}/courses", params={"course_code": "MBA-1"})
    assert client.get(f"/streams/{room['id']}/courses").json()["courses"] == ["MBA-1"]
    client.delete(f"/streams/{room['id']}/courses/MBA-1")
    assert client.get(f"/streams/{room['id']}/courses").json()["courses"] == []


def test_identify_and_detect_only(client, env):
    eng = env["engine"]
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    img = jpeg_bytes(seed=11)
    eng.match[img] = ("AT00001", 0.77)
    r = client.post("/recognition/identify", files=[("image", _upload("x.jpg", img))]).json()
    assert r["student"]["full_name"] == "Asha" and r["confidence"] == 0.77 and r["face_count"] == 1
    r = client.post("/recognition/identify", files=[("image", _upload("x.jpg", jpeg_bytes(seed=12)))]).json()
    assert r["student"] is None and r["face_count"] == 0
    assert client.post("/recognition/identify", files=[("image", _upload("x.txt", b"nope"))]).status_code == 400
    eng.default_faces = [[1, 2, 3, 4]]
    assert client.post("/recognition/detect_only", files=[("image", _upload("x.jpg", img))]).json() == {"face_count": 1, "boxes": [[1, 2, 3, 4]]}
