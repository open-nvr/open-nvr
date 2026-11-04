"""The class worker: snapshots from the camera source, detect → crop →
recognise per face, slots aggregated, class finalised."""
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np

from attendly.services.engine import StreamEngine, StreamWorker
from attendly.services.recognize import crop_boxes, recognize_codes_in_frame
from attendly.storage import repo


def _frame(w=300, h=200):
    return np.full((h, w, 3), 100, dtype=np.uint8)


def test_crop_boxes_adds_margin_and_clamps():
    frame = _frame()
    faces = [{"bbox_px": [0, 0, 20, 20]}, {"bbox_px": [280, 180, 299, 199]}, {"bbox_px": [50, 50, 50, 60]}]
    crops = crop_boxes(frame, faces)
    assert [i for i, _ in crops] == [0, 1]
    assert crops[0][1].shape[0] >= 20 and crops[1][1].shape[1] <= 22
    # a normalised box works too
    crops = crop_boxes(frame, [{"BoundingBox": {"Left": 0.5, "Top": 0.5, "Width": 0.1, "Height": 0.1}}])
    assert crops[0][1].shape[1] in range(30, 40)


def test_recognize_codes_filters_to_students_course_and_section(env):
    eng = env["engine"]
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    repo.put_student("AT00002", "Bilal", None, None, course_code="MBA-1", section="B")
    frame = _frame()
    full = cv2.imencode(".jpg", frame)[1].tobytes()
    eng.faces_in[full] = [[10, 10, 60, 60], [100, 10, 150, 60], [200, 10, 250, 60]]
    answers = iter([[("AT00001", 0.9)], [("AT00002", 0.9)], [("GHOST", 0.9)]])
    eng.search_faces = lambda *a, **k: next(answers)
    assert recognize_codes_in_frame(eng, frame, course_code="MBA-1", section="A") == ["AT00001"]
    answers = iter([[("AT00001", 0.9)], [("AT00002", 0.9)], [("GHOST", 0.9)]])
    assert recognize_codes_in_frame(eng, frame) == ["AT00001", "AT00002"]
    assert recognize_codes_in_frame(None, frame) == [] and recognize_codes_in_frame(eng, None) == []


def test_worker_runs_a_class_on_a_fake_clock(env):
    eng, cams = env["engine"], env["cameras"]
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    repo.put_student("AT00002", "Bilal", None, None, course_code="MBA-1", section="A")
    room = repo.streams_put("Lab 1", camera_id=7, camera_name="Lab 1 camera")
    frame = _frame()
    cams.frames[7] = frame
    eng.default_faces = [[10, 10, 60, 60]]
    eng.search_faces = lambda *a, **k: [("AT00001", 0.9)]

    start = datetime(2026, 1, 5, 3, 30, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    now = {"t": start}
    w = StreamWorker(stream_id=room["id"], name="Lab 1", camera_id=7, class_id="MBA-1-A-20260105-0900-0920",
                     course_code="MBA-1", section="A", start_time=start, end_time=end, recognizer=eng, frames=cams,
                     clock=lambda: now["t"], tick_seconds=0.0)
    # Drive the loop by hand: each call to _run would block, so step time
    # with a thread and a tiny tick.
    import threading

    def advance():
        for minutes in range(0, 22):
            now["t"] = start + timedelta(minutes=minutes)
            threading.Event().wait(0.01)

    th = threading.Thread(target=w._run)
    th.start()
    advance()
    th.join(timeout=5)
    assert not th.is_alive()
    # base_slot 10 min, attempt_interval 2 min → 2 slots, ~5 attempts each
    summary = repo.snapshot_summary("MBA-1-A-20260105-0900-0920")
    assert summary["total_slots"] == 2
    assert summary["counts"] == {"AT00001": 2}
    assert w.attempts >= 6 and cams.snapshots >= 6
    res = repo.finalize_attendance("MBA-1-A-20260105-0900-0920")
    rows = {r["student_code"]: r["status"] for r in repo.get_attendance_by_class("MBA-1-A-20260105-0900-0920")}
    assert res["total_slots"] == 2 and rows == {"AT00001": "PRESENT", "AT00002": "ABSENT"}


def test_engine_reload_and_status(env):
    room = repo.streams_put("Lab 1", camera_id=7)
    repo.streams_update(room["id"], schedule=[{"class_id": "C-A-20260105-0900-1000", "start_iso": "2026-01-05T09:00:00+00:00",
                                               "end_iso": "2026-01-05T10:00:00+00:00"}])
    eng = StreamEngine(recognizer=env["engine"], frames=env["cameras"])
    eng.reload_streams()
    assert list(eng.schedules().keys()) == [room["id"]]
    assert eng.schedules()[room["id"]][0]["is_finalized"] is False
    assert eng.status() == []
    # a past class is finalised on the next reschedule pass, once
    eng._aps = None
    eng._finalize("C-A-20260105-0900-1000", room["id"])
    assert repo.list_attendance_classes() == ["C-A-20260105-0900-1000"]
    assert repo.streams_get(room["id"])["schedule"][0]["is_finalized"] is True


def test_engine_start_and_shutdown_return_promptly(env):
    """start() once deadlocked on its own lock (reload under the lock)."""
    import threading
    import time

    room = repo.streams_put("Lab 1", camera_id=7)
    repo.streams_update(room["id"], schedule=[{"class_id": "C-A-20260105-0900-1000", "start_iso": "2026-01-05T09:00:00+00:00",
                                               "end_iso": "2026-01-05T10:00:00+00:00"}])
    eng = StreamEngine(recognizer=env["engine"], frames=env["cameras"])
    done = threading.Event()

    def run():
        eng.start()
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t0 = time.monotonic()
    t.start()
    assert done.wait(5.0), "StreamEngine.start() did not return"
    assert time.monotonic() - t0 < 5.0
    assert list(eng.schedules().keys()) == [room["id"]]
    # the missed class was finalised during the initial load
    assert repo.list_attendance_classes() == ["C-A-20260105-0900-1000"]
    eng.start()  # idempotent
    eng.shutdown()
