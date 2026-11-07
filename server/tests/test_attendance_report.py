# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The attendance report (routers/attendance.py → services/attendance_report.py).

* person × final session, in the group's own timezone, with counts and
  a rate that leaves out excused marks and sessions nobody could watch;
* CSV and XLSX downloads, safe against formula injection, audited;
* a partner API token with attendance.view reads groups, sessions and the
  report, and nothing else.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta

from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture
from tests.test_attendance_sessions import _attempt, _setup, _tick

URL = "/api/v1/attendance"
T0 = datetime(2026, 11, 2, 3, 30, tzinfo=UTC)          # Monday 09:00 IST


def _week(env):  # noqa: F811
    """Two Mondays of CS-A. Week 1: Asha seen throughout, Bilal never.
    Week 2: the camera is dead all session (no coverage)."""
    _, p, _ = _setup(env, members=("Asha", "Bilal", "=cmd|' /C calc'!A0"))
    _tick(env, T0 - timedelta(hours=1))
    _tick(env, T0 + timedelta(seconds=30))
    for b in range(10):
        _attempt(env, T0 + timedelta(minutes=b * 5 + 1), [(p["Asha"], 0.9)])
    _tick(env, T0 + timedelta(minutes=56))
    week2 = T0 + timedelta(days=7)
    _tick(env, week2 - timedelta(hours=1))
    _tick(env, week2 + timedelta(minutes=56))
    return p


def test_the_report_counts_marks_and_leaves_out_what_nobody_watched(env):  # noqa: F811
    p = _week(env)
    s = env.Session()
    sid1 = s.query(env.models.AttendanceSession).order_by(env.models.AttendanceSession.id).first().id
    group_id = s.query(env.models.AttendanceGroup).one().id
    s.close()
    # Bilal was excused in week 1.
    env.client.patch(f"{URL}/sessions/{sid1}/marks/{p['Bilal']['id']}", headers=env.jwt("admin"),
                     json={"status": "EXCUSED"})

    r = env.client.get(f"{URL}/report?group_id={group_id}&from=2026-11-01&to=2026-11-30",
                       headers=env.jwt("admin"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(c["local_date"], c["start"], c["no_coverage"]) for c in body["sessions"]] == [
        ("2026-11-02", "09:00", False), ("2026-11-09", "09:00", True)]
    rows = {row["display_name"]: row for row in body["people"]}
    asha, bilal = rows["Asha"], rows["Bilal"]
    # Week 2's automatic ABSENT is not counted: nobody watched that room.
    assert (asha["present"], asha["absent"], asha["rate"]) == (1, 0, 1.0)
    assert (bilal["excused"], bilal["absent"], bilal["rate"]) == (1, 0, None)
    assert asha["marks"][str(body["sessions"][1]["id"])] == "ABSENT"   # shown, not counted


def test_a_manual_mark_on_a_blind_session_does_count(env):  # noqa: F811
    p = _week(env)
    s = env.Session()
    sid2 = s.query(env.models.AttendanceSession).order_by(
        env.models.AttendanceSession.id.desc()).first().id
    gid = s.query(env.models.AttendanceGroup).one().id
    s.close()
    env.client.patch(f"{URL}/sessions/{sid2}/marks/{p['Bilal']['id']}", headers=env.jwt("admin"),
                     json={"status": "PRESENT", "note": "teacher saw him"})
    rows = {row["display_name"]: row for row in env.client.get(
        f"{URL}/report?group_id={gid}&from=2026-11-01&to=2026-11-30",
        headers=env.jwt("admin")).json()["people"]}
    assert (rows["Bilal"]["present"], rows["Bilal"]["absent"], rows["Bilal"]["rate"]) == (1, 1, 0.5)


def test_csv_and_xlsx_downloads_are_safe_and_audited(env):  # noqa: F811
    _week(env)
    s = env.Session()
    gid = s.query(env.models.AttendanceGroup).one().id
    s.close()
    q = f"{URL}/report?group_id={gid}&from=2026-11-01&to=2026-11-30"
    r = env.client.get(q + "&format=csv", headers=env.jwt("admin"))
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    table = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert table[0][:4] == ["Name", "Reference", "Email", "2026-11-02 09:00"]
    assert table[0][4] == "2026-11-09 09:00 (no coverage)"
    names = [row[0] for row in table[1:]]
    assert "'=cmd|' /C calc'!A0" in names           # quoted, never a formula
    assert ["Asha", "R0", "", "P", "A", "1", "0", "0", "0", "100.0"] in table

    x = env.client.get(q + "&format=xlsx", headers=env.jwt("admin"))
    assert x.status_code == 200 and x.content[:2] == b"PK"
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(x.content)).active
    assert ws["A1"].value == "Name" and ws.max_row == 4

    s = env.Session()
    formats = [d for (d,) in s.query(env.models.AuditLog.details)
               .filter(env.models.AuditLog.action == "attendance.report").all()]
    s.close()
    assert len(formats) == 2 and '"format": "csv"' in formats[0]


def test_report_range_is_checked(env):  # noqa: F811
    g, _, _ = _setup(env, members=("Asha",))
    A = env.jwt("admin")
    assert env.client.get(f"{URL}/report?group_id={g['id']}&from=2026-11-10&to=2026-11-01",
                          headers=A).status_code == 422
    assert env.client.get(f"{URL}/report?group_id={g['id']}&from=2025-01-01&to=2026-11-01",
                          headers=A).status_code == 422
    assert env.client.get(f"{URL}/report?group_id=999&from=2026-11-01&to=2026-11-02",
                          headers=A).status_code == 404
    assert env.client.get(f"{URL}/report?group_id={g['id']}&from=2026-11-01&to=2026-11-02"
                          "&format=pdf", headers=A).status_code == 422


def test_a_partner_token_reads_attendance_and_nothing_else(env):  # noqa: F811
    _week(env)
    s = env.Session()
    gid = s.query(env.models.AttendanceGroup).one().id
    sid = s.query(env.models.AttendanceSession).first().id
    s.close()
    tok = _as(_mint(env, scopes=["attendance.view"])["token"])
    assert env.client.get(f"{URL}/groups", headers=tok).status_code == 200
    assert env.client.get(f"{URL}/sessions/{sid}", headers=tok).status_code == 200
    r = env.client.get(f"{URL}/report?group_id={gid}&from=2026-11-01&to=2026-11-30", headers=tok)
    assert r.status_code == 200 and len(r.json()["people"]) == 3
    # Not the roster, not a correction, not the cameras.
    assert env.client.get(f"{URL}/people", headers=tok).status_code == 403
    assert env.client.patch(f"{URL}/sessions/{sid}/marks/1", headers=tok,
                            json={"status": "PRESENT"}).status_code == 403
    assert env.client.get("/api/v1/cameras/", headers=tok).status_code == 403

    other = _as(_mint(env, name="no-attendance", scopes=["cameras.view"])["token"])
    assert env.client.get(f"{URL}/report?group_id={gid}&from=2026-11-01&to=2026-11-30",
                          headers=other).status_code == 403


def test_local_dates_follow_the_group_timezone(env):  # noqa: F811
    """A late class in Los Angeles is a different UTC date; the report's
    range is the group's own calendar."""
    A = env.jwt("admin")
    g = env.client.post(f"{URL}/groups", headers=A,
                        json={"name": "Night", "timezone": "America/Los_Angeles"}).json()
    env.client.post(f"{URL}/timetable", headers=A, json={
        "group_id": g["id"], "weekday": 0, "start_time": "20:00", "end_time": "21:00",
        "camera_ids": [1]})
    start = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)   # Mon 20:00 PST = Tue 04:00 UTC
    _tick(env, start - timedelta(hours=1))
    _tick(env, start + timedelta(minutes=70))
    body = env.client.get(f"{URL}/report?group_id={g['id']}&from=2026-11-02&to=2026-11-02",
                          headers=A).json()
    assert [(c["local_date"], c["start"]) for c in body["sessions"]] == [("2026-11-02", "20:00")]
    assert date.fromisoformat(body["sessions"][0]["local_date"]).weekday() == 0


def test_tab_and_cr_cannot_start_a_formula():
    from services.attendance_report import _safe_cell

    assert _safe_cell("\t=1+1") == "'\t=1+1" and _safe_cell("\r@x") == "'\r@x"
    assert _safe_cell("Asha") == "Asha" and _safe_cell(3) == 3
