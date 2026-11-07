"""Marks, taken from PR #791: LATE, corrections that survive a recompute,
the coverage floor, the frozen roster, call-off and one-off classes, the
evidence frames, and the class review."""
import io
from datetime import datetime, timedelta, timezone

import numpy as np

from attendly.storage import repo
from tests.conftest import jpeg_bytes

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
CLASS = "MBA-1-A-20260105-0900-1000"


def _roster():
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    repo.put_student("AT00002", "Bilal", None, None, course_code="MBA-1", section="A")
    repo.put_student("AT00003", "Chen", None, None, course_code="MBA-1", section="A")


def _slots(seen_at_by_slot):
    """``[(slot_minutes, {code: minutes_first_seen})]`` → snapshots."""
    for slot_min, seen in seen_at_by_slot:
        repo.save_snapshot(CLASS, {c: (T0 + timedelta(minutes=m)).isoformat() for c, m in seen.items()},
                           T0 + timedelta(minutes=slot_min))


def test_late_is_first_seen_after_the_grace(env):
    _roster()
    # Asha from the start; Bilal first at minute 14 (grace is 10); Chen never
    _slots([(10, {"AT00001": 1}), (20, {"AT00001": 12, "AT00002": 14}), (30, {"AT00001": 21, "AT00002": 22}),
            (40, {"AT00001": 31, "AT00002": 33}), (50, {"AT00001": 41, "AT00002": 44}), (60, {"AT00001": 51, "AT00002": 55})])
    res = repo.finalize_attendance(CLASS)
    assert res["by_status"] == {"PRESENT": 1, "LATE": 1, "ABSENT": 1}
    rows = {r["student_code"]: r for r in repo.get_attendance_by_class(CLASS)}
    assert rows["AT00002"]["status"] == "LATE" and rows["AT00002"]["first_seen"].startswith("2026-01-05T09:14")
    assert rows["AT00001"]["status"] == "PRESENT" and rows["AT00001"]["first_seen"].startswith("2026-01-05T09:01")
    assert rows["AT00003"]["first_seen"] is None


def test_manual_mark_survives_a_recompute_and_auto_hands_it_back(env):
    _roster()
    _slots([(10, {"AT00001": 1}), (20, {"AT00001": 11})])
    repo.finalize_attendance(CLASS)
    row = repo.set_mark(CLASS, "AT00003", "EXCUSED", "medical leave")
    assert row["status"] == "EXCUSED" and row["source"] == "manual" and row["note"] == "medical leave"
    repo.finalize_attendance(CLASS)  # a recompute keeps the correction
    rows = {r["student_code"]: r for r in repo.get_attendance_by_class(CLASS)}
    assert rows["AT00003"]["status"] == "EXCUSED" and rows["AT00003"]["source"] == "manual"
    assert rows["AT00003"]["total_slots"] == 2  # counts are refreshed, the status is not
    back = repo.set_mark(CLASS, "AT00003", "AUTO")
    assert back["status"] == "ABSENT" and back["source"] == "auto"
    try:
        repo.set_mark(CLASS, "AT00003", "MAYBE")
    except ValueError as e:
        assert "AUTO" in str(e)
    else:
        raise AssertionError("an unknown status was accepted")


def test_roster_is_frozen_at_first_finalisation(env):
    _roster()
    _slots([(10, {"AT00001": 1})])
    repo.finalize_attendance(CLASS)
    assert len(repo.get_attendance_by_class(CLASS)) == 3
    repo.put_student("AT00004", "Dev", None, None, course_code="MBA-1", section="A")  # joined later
    repo.finalize_attendance(CLASS)
    assert {r["student_code"] for r in repo.get_attendance_by_class(CLASS)} == {"AT00001", "AT00002", "AT00003"}


def test_called_off_class_is_left_alone_and_restored(env):
    _roster()
    room = repo.streams_put("Lab 1", camera_id=7)
    repo.streams_update(room["id"], schedule=[{"class_id": CLASS, "start_iso": T0.isoformat(),
                                               "end_iso": (T0 + timedelta(hours=1)).isoformat()}])
    _slots([(10, {"AT00001": 1})])
    repo.finalize_attendance(CLASS)
    repo.set_class_called_off(CLASS, True)
    assert repo.streams_set_class_called_off(CLASS, True) is True
    assert repo.finalize_attendance(CLASS) == {"ok": False, "class_id": CLASS, "reason": "called_off", "written": 0, "total_slots": 0}
    assert repo.class_meta(CLASS)["called_off"] is True
    assert repo.list_scheduled_classes()[0]["called_off"] is True
    repo.set_class_called_off(CLASS, False)
    assert repo.finalize_attendance(CLASS)["ok"] is True


def test_engine_skips_and_stops_a_called_off_class(env):
    from attendly.services.engine import StreamEngine, StreamWorker

    room = repo.streams_put("Lab 1", camera_id=7)
    repo.streams_update(room["id"], schedule=[{"class_id": CLASS, "start_iso": T0.isoformat(),
                                               "end_iso": (T0 + timedelta(hours=1)).isoformat(), "called_off": True}])
    eng = StreamEngine(recognizer=env["engine"], frames=env["cameras"])
    w = StreamWorker(stream_id=room["id"], name="Lab 1", camera_id=7, class_id=CLASS, start_time=T0,
                     end_time=T0 + timedelta(hours=1), recognizer=env["engine"], frames=env["cameras"],
                     clock=lambda: T0 + timedelta(minutes=5), tick_seconds=0.01)
    eng._workers[room["id"]] = w
    w.start()
    eng.reload_streams()  # sees the flag: stops the worker
    w.thread.join(timeout=3)
    assert not w.running
    eng._aps = None
    eng._reschedule_aps()
    assert repo.class_meta(CLASS) is None  # never finalised: not even an ABSENT row


def test_evidence_frame_kept_once_per_slot(env):
    import cv2

    from attendly.services.engine import StreamWorker

    _roster()
    cams, eng = env["cameras"], env["engine"]
    frame = np.full((200, 300, 3), 90, dtype=np.uint8)
    cams.frames[7] = frame
    eng.faces_in[cv2.imencode(".jpg", frame)[1].tobytes()] = [[10, 10, 60, 60]]
    eng.search_faces = lambda *a, **k: [("AT00001", 0.9)]
    now = {"t": T0}
    w = StreamWorker(stream_id=1, name="Lab 1", camera_id=7, class_id=CLASS, course_code="MBA-1", section="A",
                     start_time=T0, end_time=T0 + timedelta(minutes=20), recognizer=eng, frames=cams,
                     clock=lambda: now["t"], tick_seconds=0.0)
    w.start()
    import time

    for step in range(0, 21):
        now["t"] = T0 + timedelta(minutes=step)
        time.sleep(0.02)
    w.thread.join(timeout=3)
    ev = repo.class_evidence(CLASS)
    assert len(ev) == 2 and len(cams.evidence) == 2  # one per slot, not per attempt
    assert repo.snapshot_summary(CLASS)["first_seen"]["AT00001"] == T0.isoformat()


def test_review_corrections_call_off_and_one_off_routes(client, env):
    _roster()
    room = client.post("/classrooms/", json={"name": "Lab 1", "camera_id": 7}).json()
    _slots([(10, {"AT00001": 1}), (20, {"AT00001": 11, "AT00002": 15})])
    env["cameras"].evidence["ev/1.jpg"] = jpeg_bytes(seed=9)
    repo.save_snapshot(CLASS, {"AT00001": (T0 + timedelta(minutes=21)).isoformat()}, T0 + timedelta(minutes=30),
                       evidence_path="ev/1.jpg")
    assert client.post(f"/attendance/{CLASS}/recompute").json()["ok"] is True
    rv = client.get(f"/attendance/{CLASS}/review").json()
    assert rv["counts"] == {"PRESENT": 1, "ABSENT": 2} and rv["meta"]["no_coverage"] is False
    assert [e["index"] for e in rv["evidence"]] == [0]
    r = client.get(f"/attendance/{CLASS}/evidence/0")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get(f"/attendance/{CLASS}/evidence/5").status_code == 404
    # a correction
    r = client.put(f"/attendance/{CLASS}/marks/AT00002", json={"status": "LATE", "note": "came at 09:15"})
    assert r.status_code == 200 and r.json()["status"] == "LATE" and r.json()["source"] == "manual"
    assert client.put(f"/attendance/{CLASS}/marks/AT00002", json={"status": "SOMETHING"}).status_code == 422
    rows = {x["student"]["student_code"]: x for x in client.get(f"/attendance/{CLASS}").json()}
    assert rows["AT00002"]["status"] == "LATE" and rows["AT00002"]["note"] == "came at 09:15"
    # call off: the report leaves it out, a recompute refuses, restore brings it back
    assert client.post(f"/attendance/{CLASS}/call_off").json()["called_off"] is True
    assert client.get("/attendance/by-course/MBA-1").json() == []
    assert client.post(f"/attendance/{CLASS}/recompute").status_code == 409
    assert client.post(f"/attendance/{CLASS}/restore").json()["called_off"] is False
    assert len(client.get("/attendance/by-course/MBA-1").json()) == 3
    # a one-off class on the room
    r = client.post(f"/streams/{room['id']}/classes", json={"course": "MBA-1", "section": "A", "date": "2099-01-05",
                                                            "start": "09:00", "end": "10:00"})
    assert r.status_code == 200, r.text
    assert r.json()["class_id"] == "MBA-1-A-20990105-0900-1000"
    assert client.post(f"/streams/{room['id']}/classes", json={"course": "MBA-1", "section": "A", "date": "2099-01-05",
                                                               "start": "09:00", "end": "10:00"}).status_code == 409
    assert client.post(f"/streams/{room['id']}/classes", json={"course": "MBA-1", "section": "A", "date": "2020-01-05",
                                                               "start": "09:00", "end": "10:00"}).status_code == 400
    sched = client.get("/attendance/scheduled").json()
    assert sched[0]["class_id"] == "MBA-1-A-20990105-0900-1000" and sched[0]["room"] == "Lab 1"
