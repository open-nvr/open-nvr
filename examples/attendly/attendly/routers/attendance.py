from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Response

from .. import schemas
from ..services.streams_service import finalize_attendance as svc_finalize
from ..storage.repo import (
    class_evidence,
    class_meta,
    get_attendance_by_class,
    get_students_batch,
    list_attendance_classes,
    list_finalized_rows,
    list_scheduled_classes,
    list_snapshot_classes,
    set_class_called_off,
    set_mark,
    streams_set_class_called_off,
)
from ..utils.logging_utils import log_call

router = APIRouter()


def _rows_out(items: List[Dict[str, Any]]) -> List[schemas.FinalizedAttendanceOut]:
    students = get_students_batch([it.get("student_code") for it in items if it.get("student_code")])
    out: List[schemas.FinalizedAttendanceOut] = []
    for it in items:
        s = students.get(it.get("student_code"))
        out.append(schemas.FinalizedAttendanceOut(
            id=int(it.get("id")), class_id=str(it.get("class_id")), student=schemas.student_out(s) if s else None,
            slots_present=int(it.get("slots_present", 0)), total_slots=int(it.get("total_slots", 0)),
            ratio=float(it.get("ratio") or 0.0), status=str(it.get("status")), source=str(it.get("source") or "auto"),
            note=it.get("note"), first_seen=it.get("first_seen"), finalized_at=str(it.get("finalized_at")),
        ))
    return out


@router.get("/classes", response_model=List[str])
@log_call()
def list_classes():
    return list_attendance_classes()


@router.get("/by-name/{class_name}", response_model=List[schemas.FinalizedAttendanceOut])
@log_call()
def get_attendance_by_class_name(class_name: str, date: Optional[str] = None):
    items = get_attendance_by_class(class_name)
    if date:
        items = [it for it in items if str(it.get("finalized_at", "")).startswith(date)]
    return _rows_out(items)


def _not_called_off(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    off = {c for c in {str(r.get("class_id")) for r in rows} if (class_meta(c) or {}).get("called_off")}
    return [r for r in rows if str(r.get("class_id")) not in off]


@router.get("/by-course/{course_code}", response_model=List[schemas.FinalizedAttendanceOut])
@log_call()
def get_attendance_by_course(course_code: str, date: Optional[str] = None):
    rows = _not_called_off([it for it in list_finalized_rows() if str(it.get("class_id") or "").startswith(f"{course_code}-")])
    if date:
        rows = [it for it in rows if str(it.get("finalized_at", "")).startswith(date)]
    return _rows_out(rows)


@router.get("/section/{section_id}", response_model=List[schemas.FinalizedAttendanceOut])
@log_call()
def get_attendance_by_section(section_id: str, date: Optional[str] = None):
    rows = list_finalized_rows()
    if date:
        rows = [it for it in rows if str(it.get("finalized_at", "")).startswith(date)]
    return _rows_out(rows)


@router.post("/refinalize/all")
@log_call()
def refinalize_all_classes() -> dict:
    classes = list_snapshot_classes()
    total, results = 0, []
    for cid in classes:
        try:
            res = svc_finalize(cid)
            total += int(res.get("written", 0))  # type: ignore[arg-type]
            results.append({"class_id": cid, "written": res.get("written", 0)})
        except Exception as e:
            results.append({"class_id": cid, "error": str(e)})
    return {"ok": True, "classes": classes, "total_written": total, "results": results}


@router.get("/scheduled")
@log_call()
def scheduled_classes():
    """Every published class window: upcoming, running, decided, called
    off — with its room. The review page starts here."""
    return list_scheduled_classes()


@router.get("/{class_id}/review")
@log_call()
def class_review(class_id: str):
    """Everything a person checking a class needs: the marks with who set
    them, how often each student was seen and when first, how much of the
    class was watched, and the evidence frames."""
    rows = _rows_out(get_attendance_by_class(class_id))
    meta = class_meta(class_id)
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    evidence = [{"index": i, "slot_time": e["slot_time"]} for i, e in enumerate(class_evidence(class_id))]
    return {"class_id": class_id, "meta": meta, "counts": counts, "rows": rows, "evidence": evidence}


@router.put("/{class_id}/marks/{student_code}", response_model=schemas.FinalizedAttendanceOut)
@log_call()
def correct_mark(class_id: str, student_code: str, payload: schemas.MarkSet):
    """A person's correction. ``AUTO`` hands the row back to the cameras."""
    try:
        row = set_mark(class_id, student_code, payload.status, payload.note)
    except KeyError:
        raise HTTPException(status_code=404, detail="no mark for this student in this class")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _rows_out([row])[0]


@router.post("/{class_id}/recompute")
@log_call()
def recompute(class_id: str):
    res = svc_finalize(class_id)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=f"class is {res.get('reason', 'not recomputable')}")
    return res


def _set_called_off(class_id: str, flag: bool):
    meta = set_class_called_off(class_id, flag)
    on_a_room = streams_set_class_called_off(class_id, flag)
    try:
        from ..services.engine import StreamEngine

        StreamEngine.get_shared().reload_streams()
    except Exception:
        pass
    return {"ok": True, "class_id": class_id, "called_off": flag, "meta": meta, "scheduled": on_a_room}


@router.post("/{class_id}/call_off")
@log_call()
def call_off(class_id: str):
    """A holiday, a class that did not happen: not watched, not marked,
    in no report. Its marks (if decided) stay hidden for a restore."""
    return _set_called_off(class_id, True)


@router.post("/{class_id}/restore")
@log_call()
def restore(class_id: str):
    return _set_called_off(class_id, False)


@router.get("/{class_id}/evidence/{index}")
@log_call()
def evidence_frame(class_id: str, index: int):
    items = class_evidence(class_id)
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="no such evidence frame")
    from ..cameras import CameraSource

    data = CameraSource.get_shared().read_evidence(items[index]["path"])
    if not data:
        raise HTTPException(status_code=404, detail="the frame is no longer kept (retention)")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{class_id}", response_model=List[schemas.FinalizedAttendanceOut])
@log_call()
def get_attendance(class_id: str):
    return _rows_out(get_attendance_by_class(class_id))
