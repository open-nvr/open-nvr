"""Staff, departments, the department worker and the attendance report."""
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from attendly.services.staff_attendance import StaffWorker, in_window
from attendly.storage import staff_repo as repo
from attendly.storage.repo import put_student
from tests.conftest import jpeg_bytes

TZ = ZoneInfo("Asia/Kolkata")


def at(hhmm: str, day: str = "2026-11-05") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=TZ)


def test_in_window_including_overnight():
    assert in_window("09:00", "18:00", at("09:00"))
    assert not in_window("09:00", "18:00", at("18:00"))
    assert in_window("22:00", "06:00", at("23:30")) and in_window("22:00", "06:00", at("02:00"))
    assert not in_window("22:00", "06:00", at("12:00"))


def test_staff_worker_marks_first_and_last_seen(env):
    eng, cams = env["engine"], env["cameras"]
    dep = repo.department_put("Physics", camera_id=9, camera_name="Hall camera", work_start="09:00", work_end="18:00")
    repo.put_staff("ST00001", "Dr Rao", department_id=dep["id"])
    repo.put_staff("ST00002", "Dr Nair", department_id=dep["id"])
    put_student("AT00001", "A Student", None, "s@example.com", course_code="X", section="A")
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    frame[20:120, 20:120] = 180
    frame[20:120, 180:280] = 60
    cams.frames[9] = frame
    eng.default_faces = [[20, 20, 120, 120], [180, 20, 280, 120]]
    from attendly.services.recognize import crop_boxes

    crops = {i: cv2.imencode(".jpg", c)[1].tobytes() for i, c in crop_boxes(frame, eng.detect_faces(cv2.imencode(".jpg", frame)[1].tobytes()))}
    eng.match[crops[0]] = ("ST00001", 0.9)
    eng.match[crops[1]] = ("AT00001", 0.9)  # a student in the staff room is not staff attendance

    clock = {"now": at("08:30")}
    w = StaffWorker(department=dep, recognizer=eng, frames=cams, clock=lambda: clock["now"])
    assert w.tick() == [] and w.polls == 0  # before the window: no look
    clock["now"] = at("09:05")
    assert w.tick() == ["ST00001"]
    clock["now"] = at("17:40")
    assert w.tick() == ["ST00001"]
    rows = repo.staff_attendance_for_day("2026-11-05", dep["id"])
    rao = next(r for r in rows if r["staff_code"] == "ST00001")
    nair = next(r for r in rows if r["staff_code"] == "ST00002")
    assert rao["status"] == "PRESENT" and rao["first_seen"].startswith("2026-11-05T09:05") and rao["last_seen"].startswith("2026-11-05T17:40")
    assert rao["sightings"] == 2
    assert nair["status"] == "ABSENT" and nair["first_seen"] is None
    assert repo.staff_attendance_dates() == ["2026-11-05"]
    assert w.status()["window"] == "09:00-18:00" and w.status()["active"] is True


def test_staff_and_departments_api(client, env):
    eng = env["engine"]
    r = client.post("/departments/", json={"name": "Physics", "camera_id": 9, "work_start": "08:30", "work_end": "17:30"})
    assert r.status_code == 200, r.text
    dep = r.json()
    assert dep["camera_name"] == "Hall camera" and dep["work_start"] == "08:30"
    assert client.post("/departments/", json={"name": "x", "camera_id": 9, "work_start": "9:00"}).status_code == 422
    # enrol a staff member from photos (the row exists only after a face was accepted)
    r = client.post("/staff/register_new", data={"full_name": "Dr Rao", "email": "rao@example.com",
                                                 "department_id": str(dep["id"]), "designation": "Professor"},
                    files=[("images", ("a.jpg", io.BytesIO(jpeg_bytes(seed=1)), "image/jpeg")),
                           ("images", ("b.jpg", io.BytesIO(jpeg_bytes(seed=2)), "image/jpeg"))])
    assert r.status_code == 200, r.text
    body = r.json()
    code = body["staff"]["staff_code"]
    assert code == "ST00001" and body["num_faces_registered"] == 2 and body["staff"]["department_name"] == "Physics"
    assert eng.enrolled[code]["category"] == "attendly_staff" and eng.enrolled[code]["name"] == "Dr Rao"
    # students' registry does not see staff
    assert client.get("/registry/collection/info").json()["person_count"] == 0
    assert client.get(f"/staff/{code}/faces").json()["total_faces"] == 2
    # duplicate email refused; unknown department refused
    assert client.post("/staff/", json={"full_name": "Twin", "email": "rao@example.com"}).status_code == 409
    assert client.post("/staff/", json={"full_name": "Lost", "department_id": 999}).status_code == 400
    r = client.put(f"/staff/{code}", json={"designation": "HoD", "department_id": None})
    assert r.json()["designation"] == "HoD" and r.json()["department_id"] is None
    assert client.get("/staff/", params={"department_id": dep["id"]}).json() == []
    rep = client.get("/staff-attendance/", params={"date": "2026-11-05"}).json()
    assert rep["absent"] == 1 and rep["rows"][0]["staff"]["staff_code"] == code
    assert client.get("/staff-attendance/", params={"date": "2026-11-05", "department_id": 999}).status_code == 404
    assert client.get("/staff-attendance/engine_status").json()["workers"][0]["department_id"] == dep["id"]
    assert client.delete(f"/staff/{code}").json()["faces_deleted"] is True
    assert client.get("/staff/").json() == [] and code not in eng.enrolled
    assert client.delete(f"/departments/{dep['id']}").json() == {"ok": True}
    assert client.get("/departments/").json() == []


def test_admin_only_deletes_for_staff_and_gates():
    from attendly.gate import required_tier

    assert required_tier("DELETE", "/staff/ST00001") == "admin"
    assert required_tier("DELETE", "/gates/3") == "admin"
    assert required_tier("DELETE", "/departments/3") == "admin"
    assert required_tier("DELETE", "/meals/bookings/3") == "operator"
    assert required_tier("POST", "/gates/3/check") == "operator"
    assert required_tier("GET", "/staff-attendance/") == "viewer"
