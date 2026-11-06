"""Regressions for the review of PR #798 (attendance correctness)."""
import zlib
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np

from attendly.services.engine import StreamEngine, StreamWorker
from attendly.storage import repo


def _frame(w=300, h=200):
    return np.full((h, w, 3), 100, dtype=np.uint8)


def _class(env, length_min=10):
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    repo.put_student("AT00002", "Bilal", None, None, course_code="MBA-1", section="A")
    room = repo.streams_put("Lab 1", camera_id=7)
    t0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    class_id = f"MBA-1-A-20260105-0900-{(9 * 60 + length_min) // 60:02d}{(9 * 60 + length_min) % 60:02d}"
    repo.streams_update(room["id"], schedule=[{"class_id": class_id, "course": "MBA-1", "section": "A",
                                               "start_iso": t0.isoformat(),
                                               "end_iso": (t0 + timedelta(minutes=length_min)).isoformat()}])
    return room, class_id, t0


def _see(env, frame, code):
    eng = env["engine"]
    full = cv2.imencode(".jpg", frame)[1].tobytes()
    eng.faces_in[full] = [[10, 10, 60, 60]]
    eng.search_faces = lambda *a, **k: [(code, 0.9)]


# 1. The last slot of a class is counted (a 10-minute class has one slot).
def test_last_slot_counts_and_worker_exit_finalises(env):
    room, class_id, t0 = _class(env, length_min=10)
    frame = _frame()
    env["cameras"].frames[7] = frame
    _see(env, frame, "AT00001")
    now = {"t": t0}
    engine = StreamEngine.get_shared()
    engine.reload_streams()
    engine.clock_now = lambda: now["t"]
    finalised = []
    w = StreamWorker(stream_id=room["id"], name="Lab 1", camera_id=7, class_id=class_id, course_code="MBA-1",
                     section="A", start_time=t0, end_time=t0 + timedelta(minutes=10), recognizer=env["engine"],
                     frames=env["cameras"], clock=lambda: now["t"], tick_seconds=0.0,
                     on_exit=lambda: (finalised.append(1), engine._worker_exited(room["id"], class_id)))
    w.start()
    import time
    for step in range(0, 11):
        now["t"] = t0 + timedelta(minutes=step)
        time.sleep(0.02)
    w.thread.join(timeout=3)
    assert not w.running and finalised == [1]
    rows = {r["student_code"]: r for r in repo.get_attendance_by_class(class_id)}
    assert rows["AT00001"]["status"] == "PRESENT" and rows["AT00001"]["total_slots"] == 1
    assert rows["AT00002"]["status"] == "ABSENT"
    # the scheduled finalisation later changes nothing
    engine._finalize(class_id, room["id"])
    assert repo.get_attendance_by_class(class_id)[0]["status"] == "PRESENT"


# 2. An outage does not mark everyone absent.
def test_unwatched_slots_do_not_count_against_anyone(env):
    _, class_id, t0 = _class(env, length_min=30)
    env["cameras"].frames[7] = None  # core down: no frames at all
    now = {"t": t0}
    w = StreamWorker(stream_id=1, name="Lab 1", camera_id=7, class_id=class_id, course_code="MBA-1", section="A",
                     start_time=t0, end_time=t0 + timedelta(minutes=30), recognizer=env["engine"],
                     frames=env["cameras"], clock=lambda: now["t"], tick_seconds=0.0)
    w.start()
    import time
    for step in range(0, 31):
        now["t"] = t0 + timedelta(minutes=step)
        time.sleep(0.02)
    w.thread.join(timeout=3)
    assert w.failed_attempts > 0 and w.unwatched_slots == 3
    summary = repo.snapshot_summary(class_id)
    assert summary["total_slots"] == 0 and summary["unwatched_slots"] == 3
    res = repo.finalize_attendance(class_id)
    assert res["total_slots"] == 0 and res["unwatched_slots"] == 3
    assert {r["status"] for r in repo.get_attendance_by_class(class_id)} == {"UNWATCHED"}
    # a room that never got a worker finalises the same way
    _, class2, _ = _class(env, length_min=20) if False else (None, "MBA-1-A-20260106-0900-0920", None)
    repo.finalize_attendance(class2)
    assert {r["status"] for r in repo.get_attendance_by_class(class2)} == {"UNWATCHED"}
    # one watched slot among unwatched ones: the ratio is over the watched one only
    t1 = datetime(2026, 1, 7, 9, 0, tzinfo=timezone.utc)
    class3 = "MBA-1-A-20260107-0900-0930"
    repo.save_snapshot(class3, [], t1 + timedelta(minutes=10), watched=False)
    repo.save_snapshot(class3, ["AT00001"], t1 + timedelta(minutes=20))
    repo.save_snapshot(class3, [], t1 + timedelta(minutes=30), watched=False)
    repo.finalize_attendance(class3)
    rows = {r["student_code"]: r for r in repo.get_attendance_by_class(class3)}
    assert rows["AT00001"]["status"] == "PRESENT" and rows["AT00001"]["total_slots"] == 1
    assert rows["AT00002"]["status"] == "ABSENT"


# 3. Two (class, student) pairs whose 31-bit hash collides keep separate rows.
def test_colliding_attendance_rows_do_not_overwrite(env):
    def h(text):
        return zlib.crc32(text.encode()) & 0x7FFFFFFF

    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    import random

    rng = random.Random(798)
    seen = {}
    pair = None
    for i in range(400000):
        # a real class id shape, so the roster (MBA-1 / A) is parsed from it
        cid = f"MBA-1-A-{rng.randrange(10**8):08d}-{rng.randrange(10**4):04d}-{rng.randrange(10**4):04d}"
        k = h(f"{cid}:AT00001")
        if k in seen:
            pair = (seen[k], cid)
            break
        seen[k] = cid
    assert pair, "no collision found in the search space"
    a, b = pair
    t0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    repo.save_snapshot(a, ["AT00001"], t0)
    repo.save_snapshot(b, [], t0)
    repo.finalize_attendance(a)
    repo.finalize_attendance(b)
    assert repo.get_attendance_by_class(a)[0]["status"] == "PRESENT"
    assert repo.get_attendance_by_class(b)[0]["status"] == "ABSENT"
    assert repo.get_attendance_by_class(a)[0]["status"] == "PRESENT"
    ids = {r["id"] for r in repo.list_finalized_rows()}
    assert len(ids) == 2


# 4. A student can be enrolled in more than one course/section.
def test_extra_enrolments_roster_and_recognition(client, env):
    from attendly.services.recognize import recognize_codes_in_frame

    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    r = client.put("/students/AT00001/enrolments", json=[{"course_code": "ECO-2", "section": "B"},
                                                          {"course_code": "MBA-1", "section": "A"},
                                                          {"course_code": "ECO-2", "section": "B"}])
    assert r.status_code == 200 and r.json() == [{"course_code": "ECO-2", "section": "B"}]
    assert client.get("/students/").json()[0]["enrolments"] == [{"course_code": "ECO-2", "section": "B"}]
    assert repo.list_student_codes_by_course_section("ECO-2", "B") == ["AT00001"]
    assert repo.list_sections_by_course("ECO-2") == ["B"]
    frame = _frame()
    _see(env, frame, "AT00001")
    assert recognize_codes_in_frame(env["engine"], frame, course_code="ECO-2", section="B") == ["AT00001"]
    assert recognize_codes_in_frame(env["engine"], frame, course_code="ECO-2", section="C") == []
    # a second course's class marks them present
    t0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    repo.save_snapshot("ECO-2-B-20260105-0900-1000", ["AT00001"], t0)
    repo.finalize_attendance("ECO-2-B-20260105-0900-1000")
    assert client.get("/attendance/ECO-2-B-20260105-0900-1000").json()[0]["status"] == "PRESENT"
    # replacing through the student update works too, and clearing removes them
    r = client.put("/students/AT00001", json={"enrolments": []})
    assert r.json()["enrolments"] == []
    assert client.put("/students/NOPE/enrolments", json=[]).status_code == 404


# 5. No hidden limit unless configured.
def test_student_limit_is_off_by_default_and_configurable(client, env, monkeypatch):
    for i in range(3):
        assert client.post("/students/", json={"full_name": f"S{i}", "course_code": "C", "section": "A",
                                               "email": f"s{i}@example.com"}).status_code == 200
    assert client.get("/registry/stats").json()["statistics"]["max_students"] == 0
    from attendly.config import get_settings

    monkeypatch.setattr(get_settings(), "max_registered_students", 3)
    r = client.post("/students/", json={"full_name": "S9", "course_code": "C", "section": "A", "email": "s9@example.com"})
    assert r.status_code == 403 and "limit" in r.json()["detail"].lower()


# 6. The shipped overlay defaults to the README's timezone.
def test_overlay_timezone_default():
    import pathlib

    text = pathlib.Path(__file__).resolve().parents[3].joinpath("docker-compose.apps.yml").read_text()
    block = text.split("\n  attendly:\n", 1)[1].split("\n  smart-doorbell-config-init:", 1)[0]
    assert "TZ=${TZ:-Asia/Kolkata}" in block and "ATTENDLY_TIMEZONE=${ATTENDLY_TIMEZONE:-${TZ:-Asia/Kolkata}}" in block


# 7. Slash-less writes from the UI reach the routers.
def test_slashless_post_and_put_are_redirected_not_405(env, tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>ui</html>")
    monkeypatch.setenv("ATTENDLY_WEB_DIR", str(web))
    from fastapi.testclient import TestClient

    import attendly_app

    with TestClient(attendly_app.build_app(start_engine=False, register=False)) as c:
        r = c.post("/courses", json={"code": "MBA-1", "name": "MBA"})
        assert r.status_code == 200, r.text
        assert r.json()["code"] == "MBA-1"
        assert c.get("/courses").json()[0]["code"] == "MBA-1"
        room = c.post("/classrooms", json={"name": "Lab 1", "camera_id": 7}).json()
        assert c.post("/planner/schedules", json={"day_of_week": "Monday", "course": "MBA-1", "section": "A",
                                                   "room_id": room["id"], "start": "09:00", "end": "10:00"}).status_code == 200
        assert c.put(f"/classrooms/{room['id']}", json={"name": "Lab 2"}).json()["name"] == "Lab 2"
        assert c.get("/manage-courses").status_code == 200  # the SPA still serves pages
        assert c.post("/manage-courses").status_code == 405
        # a page whose path shares an API prefix: the browser (text/html) gets
        # the page, the UI's own API calls (JSON) get the router
        html = {"accept": "text/html,application/xhtml+xml"}
        for page in ("/attendance", "/gates", "/staff", "/meals", "/staff-attendance"):
            r = c.get(page, headers=html, follow_redirects=False)
            assert r.status_code == 200 and "ui" in r.text, page
        r = c.get("/courses", headers={"accept": "application/json, text/plain, */*"}, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].endswith("/courses/")
        assert c.get("/gates/status", headers=html).status_code == 200  # a real API route is unaffected
