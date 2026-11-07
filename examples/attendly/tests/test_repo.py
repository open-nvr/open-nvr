from datetime import datetime, timedelta, timezone

from attendly.storage import repo


def test_student_codes_are_sequential_and_unique(env):
    assert repo.generate_student_code() == "AT00001"
    assert repo.generate_student_code() == "AT00002"
    repo.put_student("AT00001", "Asha", None, "asha@example.com", course_code="MBA-1", section="A")
    assert repo.get_student("AT00001")["full_name"] == "Asha"
    assert repo.count_students() == 1


def test_email_uniqueness_ignores_retired_students(env):
    repo.put_student("AT00001", "Asha", None, "asha@example.com", course_code="MBA-1", section="A")
    assert repo.get_student_by_email("ASHA@example.com")["student_code"] == "AT00001"
    gone = repo.move_student_to_deleted("AT00001")
    assert gone["deleted_at"]
    assert repo.get_student("AT00001") is None
    assert repo.get_student_by_email("asha@example.com") is None
    assert repo.list_students() == []
    # history still resolves the name
    assert repo.get_students_batch(["AT00001"])["AT00001"]["full_name"] == "Asha"


def test_update_student_and_sections(env):
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    repo.put_student("AT00002", "Bilal", None, None, course_code="MBA-1", section="B")
    repo.update_student("AT00002", section="A", email="b@example.com")
    assert repo.list_sections_by_course("MBA-1") == ["A"]
    assert repo.list_student_codes_by_course_section("MBA-1", "A") == ["AT00001", "AT00002"]
    try:
        repo.update_student("NOPE", full_name="x")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_planner_courses_and_templates(env):
    repo.planner_course_put("MBA-1", 0, 0, name="Marketing", sections_list=["A", "A", "B"])
    assert repo.planner_course_get("MBA-1")["sections_list"] == ["A", "B"]
    repo.planner_course_update("MBA-1", name="Marketing 101", sections_list=["A"])
    assert repo.planner_course_list()[0]["name"] == "Marketing 101"
    assert repo.planner_course_delete("MBA-1")
    assert repo.planner_course_get("MBA-1") is None

    room = repo.streams_put("Lab 1", camera_id=7, camera_name="Lab 1 camera")
    t = repo.planner_template_put("Monday", "MBA-1", "A", room["id"], "09:00", "10:00")
    assert repo.planner_template_list("Monday")[0]["id"] == t["id"]
    assert repo.planner_template_list("Tuesday") == []
    repo.planner_template_update(t["id"], start="09:30", active=False)
    got = repo.planner_template_get(t["id"])
    assert got["start"] == "09:30" and got["active"] is False
    assert repo.planner_template_delete(t["id"])
    assert repo.planner_template_list() == []


def test_streams_schedule_and_finalized_flag(env):
    room = repo.streams_put("Lab 1", camera_id=7, camera_name="Lab 1 camera")
    assert room["url"] == "opennvr://cam7"
    repo.streams_update(room["id"], schedule=[{"class_id": "MBA-1-A-20260105-0900-1000", "start_iso": "x", "end_iso": "y"}])
    sched = repo.streams_get(room["id"])["schedule"]
    assert sched[0]["is_finalized"] is False
    assert repo.streams_mark_schedule_finalized(room["id"], "MBA-1-A-20260105-0900-1000")
    assert repo.streams_get(room["id"])["schedule"][0]["is_finalized"] is True
    assert not repo.streams_mark_schedule_finalized(room["id"], "MBA-1-A-20260105-0900-1000")
    repo.streams_soft_delete(room["id"])
    assert repo.streams_list(include_deleted=False) == []
    assert len(repo.streams_list()) == 1


def test_finalize_attendance_ratio_absent_rows_and_idempotence(env):
    for code, name in (("AT00001", "Asha"), ("AT00002", "Bilal"), ("AT00003", "Chen")):
        repo.put_student(code, name, None, None, course_code="MBA-1", section="A")
    class_id = "MBA-1-A-20260105-0900-1000"
    t0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    # 4 slots: Asha in all, Bilal in 2 (50% < 60% threshold), Chen never
    repo.save_snapshot(class_id, ["AT00001", "AT00002"], t0)
    repo.save_snapshot(class_id, ["AT00001"], t0 + timedelta(minutes=10))
    repo.save_snapshot(class_id, ["AT00001", "AT00002"], t0 + timedelta(minutes=20))
    repo.save_snapshot(class_id, ["AT00001"], t0 + timedelta(minutes=30))
    assert repo.list_snapshot_classes() == [class_id]

    res = repo.finalize_attendance(class_id)
    assert {k: res[k] for k in ("ok", "class_id", "written", "total_slots", "unwatched_slots", "no_coverage")} == {
        "ok": True, "class_id": class_id, "written": 3, "total_slots": 4, "unwatched_slots": 0, "no_coverage": False}
    rows = {r["student_code"]: r for r in repo.get_attendance_by_class(class_id)}
    assert rows["AT00001"]["status"] == "PRESENT" and rows["AT00001"]["ratio"] == 1.0
    assert rows["AT00002"]["status"] == "ABSENT" and rows["AT00002"]["slots_present"] == 2
    assert rows["AT00003"]["status"] == "ABSENT" and rows["AT00003"]["total_slots"] == 4
    assert repo.list_attendance_classes() == [class_id]

    # re-finalising overwrites rather than duplicating
    repo.finalize_attendance(class_id)
    assert len(repo.get_attendance_by_class(class_id)) == 3
    assert len(repo.list_finalized_rows()) == 3


def test_attendance_for_unfinalized_class_synthesises_absent_roster(env):
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    rows = repo.get_attendance_by_class("MBA-1-A-20260105-0900-1000")
    assert [(r["student_code"], r["status"], r["total_slots"]) for r in rows] == [("AT00001", "ABSENT", 0)]
    assert repo.parse_class_id("CS-ADV-2-B-20260105-0900-1000") == ("CS-ADV-2", "B")


def test_counts(env):
    repo.put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    repo.streams_put("Lab 1", camera_id=7)
    repo.planner_course_put("MBA-1", 0, 0)
    assert repo.counts() == {"students": 1, "rooms": 1, "courses": 1, "finalized_classes": 0,
                             "hostlers": 0, "staff": 0, "departments": 0, "gates": 0}
