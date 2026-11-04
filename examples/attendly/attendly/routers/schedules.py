from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from ..services.planner_publisher import DAYS, _class_id
from ..storage.repo import streams_get, streams_update
from ..utils.logging_utils import log_call
from ..utils.time import local_tz

router = APIRouter()


@router.post("/")
@log_call()
def create_schedule(payload: Dict[str, Any]):
    """One concrete class window on a room: today-or-next ``day_of_week``,
    or an explicit ``date``."""
    room_id = payload.get("room_id")
    course = str(payload.get("course_code", "")).strip()
    section = str(payload.get("section", "")).strip()
    start = str(payload.get("start_time", "")).strip()
    end = str(payload.get("end_time", "")).strip()
    day = str(payload.get("day_of_week", "")).strip()
    date_str = str(payload.get("date", "")).strip()
    if not course or not section:
        raise HTTPException(status_code=400, detail="course_code and section are required")
    try:
        sid = int(room_id)
    except Exception:
        raise HTTPException(status_code=400, detail="room_id is required and must be an integer")
    cur = streams_get(sid)
    if not cur or cur.get("deleted_at"):
        raise HTTPException(status_code=404, detail="room not found")
    if len(start) != 5 or start[2] != ":" or len(end) != 5 or end[2] != ":":
        raise HTTPException(status_code=400, detail="start_time/end_time must be HH:MM")
    tz = local_tz()
    if date_str:
        try:
            day_dt = datetime.fromisoformat(date_str)
        except Exception:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    else:
        if day not in DAYS:
            raise HTTPException(status_code=400, detail="day_of_week must be provided or use date")
        now = datetime.now(tz)
        delta = (DAYS.index(day) - now.weekday()) % 7
        day_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=delta)
    sdt = day_dt.replace(hour=int(start[:2]), minute=int(start[3:5]), second=0, microsecond=0, tzinfo=tz)
    edt = day_dt.replace(hour=int(end[:2]), minute=int(end[3:5]), second=0, microsecond=0, tzinfo=tz)
    if edt <= sdt:
        raise HTTPException(status_code=400, detail="end_time must be after start_time")
    cid = _class_id(course, section, sdt, start, end)
    schedule = list(cur.get("schedule") or [])
    schedule.append({"class_id": cid, "start_iso": sdt.isoformat(), "end_iso": edt.isoformat(), "course": course, "section": section})
    updated = streams_update(sid, schedule=schedule)
    try:
        from ..services.engine import StreamEngine

        StreamEngine.get_shared().reload_streams()
    except Exception:
        pass
    return {"ok": True, "class_id": cid, "room_id": sid, "stream": {"id": updated.get("id") if updated else sid}}
