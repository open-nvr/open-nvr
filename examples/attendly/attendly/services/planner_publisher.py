from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

from ..storage.repo import streams_get, streams_list, streams_update
from ..utils.logging_utils import log_call
from ..utils.time import local_tz

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _stream_exists(stream_id: int) -> bool:
    return any(int(it.get("id", 0)) == int(stream_id) for it in streams_list(include_deleted=False))


def _class_id(course: str, section: str, day_date: datetime, start: str, end: str) -> str:
    return f"{course}-{section}-{day_date.strftime('%Y%m%d')}-{start.replace(':', '')}-{end.replace(':', '')}"


def _daterange(start_date: datetime, end_date: datetime) -> List[datetime]:
    out, cur = [], start_date
    while cur <= end_date:
        out.append(cur)
        cur = cur + timedelta(days=1)
    return out


def _day_name(dt: datetime) -> str:
    return DAYS[dt.weekday()]


@log_call()
def publish(from_date: str, to_date: str, templates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Expand weekly templates into concrete class windows on each room
    between the dates (inclusive). A window already on the room (same
    class id) is not added twice."""
    tz = local_tz()
    start_dt = datetime.fromisoformat(from_date).replace(tzinfo=tz)
    end_dt = datetime.fromisoformat(to_date).replace(tzinfo=tz)
    scheduled = 0
    for day in _daterange(start_dt, end_dt):
        day_name = _day_name(day)
        if day_name == "Sunday":
            continue
        for t in templates:
            if t.get("day_of_week") != day_name or not t.get("active", True):
                continue
            sid = int(t.get("room_id", 0))
            if sid <= 0 or not _stream_exists(sid):
                continue
            start_str, end_str = str(t.get("start")), str(t.get("end"))
            sdt = day.replace(hour=int(start_str[:2]), minute=int(start_str[3:5]), second=0, microsecond=0)
            edt = day.replace(hour=int(end_str[:2]), minute=int(end_str[3:5]), second=0, microsecond=0)
            course, section = str(t.get("course")), str(t.get("section"))
            class_id = _class_id(course, section, day, start_str, end_str)
            cur = streams_get(sid)
            schedule = list(cur.get("schedule") or []) if cur else []
            if any(str(e.get("class_id")) == class_id for e in schedule):
                continue
            schedule.append({"class_id": class_id, "start_iso": sdt.isoformat(), "end_iso": edt.isoformat(),
                             "course": course, "section": section})
            streams_update(sid, schedule=schedule)
            scheduled += 1
    try:
        from .engine import StreamEngine

        StreamEngine.get_shared().reload_streams()
    except Exception:
        pass
    return {"ok": True, "scheduled": scheduled}
