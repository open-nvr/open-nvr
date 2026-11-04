# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The attendance report: one group, a date range, person x session.

What a teacher prints and what a partner system pulls. Rows are everyone
who was a member, or who has a mark in the range (people since moved on
still have a history). Columns are the group's FINAL sessions, in the
group's own timezone. Each person gets PRESENT / LATE / ABSENT / EXCUSED
counts and a rate:

    rate = (present + late) / (sessions marked - excused)

A session nobody could watch (``no_coverage``) is listed, so the gap is
visible, but its automatic marks are left out of every count and rate:
an empty room and a dead camera must not look the same. A mark a person
set by hand on such a session does count — they were there to say so.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

STATUS_LETTER = {"PRESENT": "P", "LATE": "L", "ABSENT": "A", "EXCUSED": "E"}


def _utc(dt):
    from services.attendance_scheduler import _utc as u
    return u(dt)


def build(db, group, date_from: date, date_to: date) -> dict[str, Any]:
    from models import (
        AttendanceGroupMember,
        AttendanceMark,
        AttendancePerson,
        AttendanceSession,
    )

    tz = ZoneInfo(group.timezone)
    sessions = (db.query(AttendanceSession)
                .filter(AttendanceSession.group_id == group.id,
                        AttendanceSession.status == "final",
                        AttendanceSession.local_date >= date_from,
                        AttendanceSession.local_date <= date_to)
                .order_by(AttendanceSession.start_at, AttendanceSession.id).all())
    sids = [s.id for s in sessions]
    marks = (db.query(AttendanceMark).filter(AttendanceMark.session_id.in_(sids)).all()
             if sids else [])
    by_person: dict[int, dict[int, AttendanceMark]] = {}
    for m in marks:
        by_person.setdefault(m.person_id, {})[m.session_id] = m
    member_ids = {pid for (pid,) in db.query(AttendanceGroupMember.person_id)
                  .filter(AttendanceGroupMember.group_id == group.id).all()}
    people = (db.query(AttendancePerson)
              .filter(AttendancePerson.id.in_(member_ids | set(by_person))).all()
              if (member_ids or by_person) else [])
    people.sort(key=lambda p: (p.display_name.lower(), p.id))
    blind = {s.id for s in sessions if s.no_coverage}

    cols = []
    for s in sessions:
        start, end = _utc(s.start_at).astimezone(tz), _utc(s.end_at).astimezone(tz)
        cols.append({"id": s.id, "local_date": s.local_date.isoformat(),
                     "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                     "no_coverage": bool(s.no_coverage)})
    rows = []
    for p in people:
        mine = by_person.get(p.id, {})
        counts = {"PRESENT": 0, "LATE": 0, "ABSENT": 0, "EXCUSED": 0}
        cells: dict[str, str | None] = {}
        for s in sessions:
            m = mine.get(s.id)
            cells[str(s.id)] = m.status if m else None
            if m is None or (s.id in blind and m.source != "manual"):
                continue
            counts[m.status] = counts.get(m.status, 0) + 1
        marked = sum(counts.values()) - counts["EXCUSED"]
        rate = round((counts["PRESENT"] + counts["LATE"]) / marked, 4) if marked else None
        rows.append({"person_id": p.id, "display_name": p.display_name,
                     "external_ref": p.external_ref, "email": p.email,
                     "member": p.id in member_ids, "marks": cells,
                     "present": counts["PRESENT"], "late": counts["LATE"],
                     "absent": counts["ABSENT"], "excused": counts["EXCUSED"], "rate": rate})
    return {"group": {"id": group.id, "name": group.name, "code": group.code,
                      "timezone": group.timezone},
            "from": date_from.isoformat(), "to": date_to.isoformat(),
            "sessions": cols, "people": rows}


def _table(report: dict[str, Any]) -> list[list[Any]]:
    head = ["Name", "Reference", "Email"]
    head += [f"{c['local_date']} {c['start']}" + (" (no coverage)" if c["no_coverage"] else "")
             for c in report["sessions"]]
    head += ["Present", "Late", "Absent", "Excused", "Attendance %"]
    out = [head]
    for r in report["people"]:
        line = [r["display_name"], r["external_ref"] or "", r["email"] or ""]
        line += [STATUS_LETTER.get(r["marks"].get(str(c["id"])) or "", "")
                 for c in report["sessions"]]
        line += [r["present"], r["late"], r["absent"], r["excused"],
                 "" if r["rate"] is None else round(r["rate"] * 100, 1)]
        out.append(line)
    return out


def _safe_cell(value: Any) -> Any:
    """A name like ``=HYPERLINK(...)`` must not become a formula in a
    spreadsheet: CSV/formula injection. Leading = + - @ tab and CR are
    quoted (OWASP's list)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def to_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for line in _table(report):
        w.writerow([_safe_cell(v) for v in line])
    return buf.getvalue()


def to_xlsx(report: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    table = _table(report)
    for i, line in enumerate(table):
        ws.append([_safe_cell(v) for v in line])
        if i == 0:
            for cell in ws[1]:
                cell.font = Font(bold=True)
    ws.freeze_panes = "D2"
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 26
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
