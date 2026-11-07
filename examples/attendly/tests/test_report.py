"""The student × class report (from PR #791): totals, the rate, called-off
and unwatched classes, and cells escaped against formula injection."""
from datetime import datetime, timedelta, timezone

from attendly.storage import repo
from attendly.storage.repo import _safe_cell, attendance_report, report_csv


def _setup():
    repo.put_student("AT00001", "Asha", None, "a@example.com", course_code="MBA-1", section="A")
    repo.put_student("AT00002", "=HYPERLINK(\"http://x\")", None, None, course_code="MBA-1", section="A")
    t = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    c1, c2, c3, c4 = ("MBA-1-A-20260105-0900-1000", "MBA-1-A-20260106-0900-1000", "MBA-1-A-20260107-0900-1000",
                      "MBA-1-B-20260107-1100-1200")
    repo.save_snapshot(c1, {"AT00001": t.isoformat()}, t + timedelta(minutes=10))
    repo.save_snapshot(c1, {"AT00001": t.isoformat()}, t + timedelta(minutes=20))
    repo.finalize_attendance(c1)  # Asha present, the other absent
    t2 = t + timedelta(days=1)
    repo.save_snapshot(c2, {"AT00001": (t2 + timedelta(minutes=15)).isoformat(), "AT00002": t2.isoformat()}, t2 + timedelta(minutes=20))
    repo.finalize_attendance(c2)  # Asha late, the other present
    repo.set_mark(c2, "AT00002", "EXCUSED", "sick")
    t3 = t + timedelta(days=2)
    repo.save_snapshot(c3, [], t3 + timedelta(minutes=10), watched=False)
    repo.finalize_attendance(c3)  # unwatched: shown, counts for nothing
    repo.save_snapshot(c4, ["AT00001"], t3 + timedelta(hours=2))
    repo.finalize_attendance(c4)  # another section
    return c1, c2, c3, c4


def test_report_rows_totals_rate_and_filters(env):
    c1, c2, c3, c4 = _setup()
    rep = attendance_report("MBA-1", "A")
    assert [c["class_id"] for c in rep["classes"]] == [c1, c2, c3]
    assert rep["classes"][2]["no_coverage"] is True and rep["classes"][0]["start"] == "09:00"
    rows = {r["student_code"]: r for r in rep["rows"]}
    asha = rows["AT00001"]
    assert (asha["present"], asha["late"], asha["absent"], asha["unwatched"]) == (1, 1, 0, 1)
    assert asha["rate"] == 1.0 and asha["marks"][c2] == {"status": "LATE", "source": "auto"}
    other = rows["AT00002"]
    assert (other["absent"], other["excused"], other["present"]) == (1, 1, 0) and other["rate"] == 0.0
    assert other["marks"][c2]["source"] == "manual"
    # a called-off class drops out; a date range narrows; the section filter
    repo.set_class_called_off(c1, True)
    rep = attendance_report("MBA-1", "A")
    assert [c["class_id"] for c in rep["classes"]] == [c2, c3]
    assert [c["class_id"] for c in attendance_report("MBA-1", None, "2026-01-07", "2026-01-07")["classes"]] == [c3, c4]
    assert attendance_report("ECO-9")["rows"] == []


def test_report_csv_escapes_formulas_and_marks_manual(env):
    _setup()
    rep = attendance_report("MBA-1", "A")
    csv_text = report_csv(rep)
    lines = csv_text.strip().split("\n")
    assert lines[0].startswith("Student code,Name,Section,2026-01-05 09:00,2026-01-06 09:00,2026-01-07 09:00,Present,Late,Absent,Excused,Not watched,Rate %")
    assert "'=HYPERLINK" in csv_text and "E*" in csv_text
    assert _safe_cell("+1") == "'+1" and _safe_cell("Asha") == "Asha" and _safe_cell(3) == 3


def test_report_route(client, env):
    _setup()
    r = client.get("/attendance/report", params={"course": "MBA-1", "section": "A"})
    assert r.status_code == 200 and len(r.json()["classes"]) == 3
    r = client.get("/attendance/report", params={"course": "MBA-1", "format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    assert client.get("/attendance/report", params={"course": "MBA-1", "date_from": "5 Jan"}).status_code == 400
    assert client.get("/attendance/report", params={"course": "MBA-1", "format": "pdf"}).status_code == 400
