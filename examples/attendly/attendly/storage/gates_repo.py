"""Gates (hostel blocks, the mess), their decision log, and meal bookings."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from ..utils.time import now_local_iso
from .db import session_scope
from .models import AccessEvent, Gate, HostelPresence, MealBooking

GATE_KINDS = ("hostel", "mess")
GATE_MODES = ("barrier", "monitor")
DIRECTIONS = ("in", "out", "toggle")
ALERT_ON = ("all", "deny", "none")
MEALS = ("breakfast", "lunch", "dinner")


def _now_iso() -> str:
    return now_local_iso()


# ── Gates ───────────────────────────────────────────────────────────


def _check_choice(name: str, value: str, choices: tuple) -> None:
    if value not in choices:
        raise ValueError(f"{name} must be one of {choices}")


def _gate_dict(g: Gate) -> Dict[str, Any]:
    return {
        "id": int(g.id), "name": g.name, "kind": g.kind, "camera_id": g.camera_id, "camera_name": g.camera_name,
        "hostel_block": g.hostel_block, "allow_staff": bool(g.allow_staff), "enabled": bool(g.enabled),
        "mode": g.mode or "barrier", "direction": g.direction or "in", "alert_on": g.alert_on or "all",
        "open_url": g.open_url, "meal_windows": g.meal_windows, "created_at": g.created_at,
        "updated_at": g.updated_at, "deleted_at": g.deleted_at,
    }


def gate_put(
    name: str, kind: str, *, camera_id: Optional[int], camera_name: Optional[str] = None,
    hostel_block: Optional[str] = None, allow_staff: bool = False, enabled: bool = True,
    open_url: Optional[str] = None, meal_windows: Optional[str] = None, mode: str = "barrier",
    direction: str = "in", alert_on: str = "all",
) -> Dict[str, Any]:
    if kind not in GATE_KINDS:
        raise ValueError(f"kind must be one of {GATE_KINDS}")
    _check_choice("mode", mode, GATE_MODES)
    _check_choice("direction", direction, DIRECTIONS)
    _check_choice("alert_on", alert_on, ALERT_ON)
    with session_scope() as db:
        ts = _now_iso()
        row = Gate(
            name=name, kind=kind, camera_id=int(camera_id) if camera_id is not None else None,
            camera_name=camera_name, hostel_block=(hostel_block or "").strip() or None if kind == "hostel" else None,
            allow_staff=bool(allow_staff), enabled=bool(enabled), open_url=(open_url or "").strip() or None,
            mode=mode, direction=direction, alert_on=alert_on,
            meal_windows=(meal_windows or "").strip() or None if kind == "mess" else None,
            created_at=ts, updated_at=ts,
        )
        db.add(row)
        db.flush()
        return _gate_dict(row)


def gate_get(gate_id: int) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(Gate, int(gate_id))
        if row is None or row.deleted_at:
            return None
        return _gate_dict(row)


def gate_list(enabled_only: bool = False) -> List[Dict[str, Any]]:
    with session_scope() as db:
        stmt = select(Gate).where(Gate.deleted_at.is_(None))
        if enabled_only:
            stmt = stmt.where(Gate.enabled.is_(True))
        rows = db.execute(stmt.order_by(Gate.kind, Gate.name, Gate.id)).scalars().all()
        return [_gate_dict(r) for r in rows]


def gate_update(gate_id: int, **fields) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(Gate, int(gate_id))
        if row is None or row.deleted_at:
            return None
        if fields.get("name") is not None:
            row.name = str(fields["name"])
        if fields.get("kind") is not None:
            if fields["kind"] not in GATE_KINDS:
                raise ValueError(f"kind must be one of {GATE_KINDS}")
            row.kind = str(fields["kind"])
        if fields.get("camera_id") is not None:
            row.camera_id = int(fields["camera_id"])
        if "camera_name" in fields and fields["camera_name"] is not None:
            row.camera_name = fields["camera_name"]
        if "hostel_block" in fields and fields["hostel_block"] is not None:
            row.hostel_block = str(fields["hostel_block"]).strip() or None
        if fields.get("allow_staff") is not None:
            row.allow_staff = bool(fields["allow_staff"])
        for name, choices in (("mode", GATE_MODES), ("direction", DIRECTIONS), ("alert_on", ALERT_ON)):
            if fields.get(name) is not None:
                _check_choice(name, str(fields[name]), choices)
                setattr(row, name, str(fields[name]))
        if fields.get("enabled") is not None:
            row.enabled = bool(fields["enabled"])
        if "open_url" in fields and fields["open_url"] is not None:
            row.open_url = str(fields["open_url"]).strip() or None
        if "meal_windows" in fields and fields["meal_windows"] is not None:
            row.meal_windows = str(fields["meal_windows"]).strip() or None
        if row.kind != "hostel":
            row.hostel_block = None
        if row.kind != "mess":
            row.meal_windows = None
        row.updated_at = _now_iso()
        db.flush()
        return _gate_dict(row)


def gate_delete(gate_id: int) -> bool:
    with session_scope() as db:
        row = db.get(Gate, int(gate_id))
        if row is None or row.deleted_at:
            return False
        row.deleted_at = row.updated_at = _now_iso()
        return True


# ── Decision log ────────────────────────────────────────────────────


def _event_dict(e: AccessEvent) -> Dict[str, Any]:
    return {
        "id": int(e.id), "gate_id": int(e.gate_id), "gate_name": e.gate_name, "kind": e.kind,
        "person_code": e.person_code, "person_name": e.person_name, "person_kind": e.person_kind,
        "decision": e.decision, "reason": e.reason, "unit": e.unit, "similarity": e.similarity,
        "actuated": bool(e.actuated), "source": e.source, "movement": e.movement, "alerted": bool(e.alerted), "ts": e.ts,
    }


def access_event_put(
    *, gate: Dict[str, Any], decision: str, reason: str, person_code: Optional[str], person_name: Optional[str],
    person_kind: Optional[str], unit: Optional[str], similarity: Optional[float], actuated: bool,
    source: str = "camera", ts: Optional[str] = None, movement: Optional[str] = None, alerted: bool = False,
) -> Dict[str, Any]:
    with session_scope() as db:
        row = AccessEvent(
            gate_id=int(gate["id"]), gate_name=gate.get("name"), kind=str(gate.get("kind") or ""),
            person_code=person_code, person_name=person_name, person_kind=person_kind, decision=decision,
            reason=reason, unit=unit, similarity=float(similarity) if similarity is not None else None,
            actuated=bool(actuated), source=source, ts=ts or _now_iso(), movement=movement, alerted=bool(alerted),
        )
        db.add(row)
        db.flush()
        return _event_dict(row)


def access_events_list(
    gate_id: Optional[int] = None, *, limit: int = 100, date: Optional[str] = None,
    decision: Optional[str] = None,
) -> List[Dict[str, Any]]:
    with session_scope() as db:
        stmt = select(AccessEvent)
        if gate_id is not None:
            stmt = stmt.where(AccessEvent.gate_id == int(gate_id))
        if date:
            stmt = stmt.where(AccessEvent.ts.startswith(date))
        if decision:
            stmt = stmt.where(AccessEvent.decision == decision)
        rows = db.execute(stmt.order_by(AccessEvent.id.desc()).limit(max(1, min(int(limit), 1000)))).scalars().all()
        return [_event_dict(r) for r in rows]


def access_counts(date: str) -> Dict[str, int]:
    with session_scope() as db:
        allowed = int(db.execute(select(func.count()).select_from(AccessEvent).where(
            AccessEvent.ts.startswith(date), AccessEvent.decision == "allow")).scalar() or 0)
        denied = int(db.execute(select(func.count()).select_from(AccessEvent).where(
            AccessEvent.ts.startswith(date), AccessEvent.decision != "allow")).scalar() or 0)
    return {"allowed": allowed, "denied": denied}


def access_event_mark_alerted(event_id: int) -> None:
    with session_scope() as db:
        row = db.get(AccessEvent, int(event_id))
        if row is not None:
            row.alerted = True


# ── Hostel presence (who is in) ─────────────────────────────────────


def _presence_dict(p: HostelPresence) -> Dict[str, Any]:
    return {"student_code": p.student_code, "hostel_block": p.hostel_block, "status": p.status, "since": p.since,
            "gate_id": p.gate_id, "gate_name": p.gate_name}


def presence_get(student_code: str) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(HostelPresence, student_code)
        return _presence_dict(row) if row else None


def presence_set(student_code: str, status: str, *, hostel_block: Optional[str], ts: Optional[str] = None,
                 gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if status not in ("in", "out"):
        raise ValueError("status must be in or out")
    with session_scope() as db:
        row = db.get(HostelPresence, student_code)
        if row is None:
            row = HostelPresence(student_code=student_code, since=ts or _now_iso())
            db.add(row)
        row.status = status
        row.since = ts or _now_iso()
        row.hostel_block = hostel_block
        row.gate_id = int(gate["id"]) if gate else None
        row.gate_name = gate.get("name") if gate else None
        db.flush()
        return _presence_dict(row)


def presence_list(hostel_block: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    with session_scope() as db:
        stmt = select(HostelPresence)
        if hostel_block:
            stmt = stmt.where(HostelPresence.hostel_block == hostel_block)
        if status:
            stmt = stmt.where(HostelPresence.status == status)
        rows = db.execute(stmt.order_by(HostelPresence.since.desc())).scalars().all()
        return [_presence_dict(r) for r in rows]


def presence_summary() -> Dict[str, Dict[str, int]]:
    """``{block: {"in": n, "out": n}}`` over students with a presence row."""
    out: Dict[str, Dict[str, int]] = {}
    with session_scope() as db:
        rows = db.execute(select(HostelPresence.hostel_block, HostelPresence.status)).all()
    for block, status in rows:
        b = out.setdefault(str(block or "?"), {"in": 0, "out": 0})
        if status in b:
            b[status] += 1
    return out


# ── Meal bookings ───────────────────────────────────────────────────


def _booking_dict(b: MealBooking) -> Dict[str, Any]:
    return {
        "id": int(b.id), "student_code": b.student_code, "date": b.date, "meal": b.meal, "source": b.source,
        "created_at": b.created_at, "consumed_at": b.consumed_at, "consumed_gate_id": b.consumed_gate_id,
    }


def meal_booking_put(student_code: str, date: str, meal: str, *, source: str = "manual") -> tuple[Dict[str, Any], bool]:
    """Idempotent: ``(row, created)``."""
    if meal not in MEALS:
        raise ValueError(f"meal must be one of {MEALS}")
    with session_scope() as db:
        row = db.execute(select(MealBooking).where(
            MealBooking.student_code == student_code, MealBooking.date == date, MealBooking.meal == meal
        )).scalar_one_or_none()
        if row is not None:
            return _booking_dict(row), False
        row = MealBooking(student_code=student_code, date=date, meal=meal, source=source, created_at=_now_iso())
        db.add(row)
        db.flush()
        return _booking_dict(row), True


def meal_booking_get(student_code: str, date: str, meal: str) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.execute(select(MealBooking).where(
            MealBooking.student_code == student_code, MealBooking.date == date, MealBooking.meal == meal
        )).scalar_one_or_none()
        return _booking_dict(row) if row else None


def meal_bookings_list(date: str, meal: Optional[str] = None, student_code: Optional[str] = None) -> List[Dict[str, Any]]:
    with session_scope() as db:
        stmt = select(MealBooking).where(MealBooking.date == date)
        if meal:
            stmt = stmt.where(MealBooking.meal == meal)
        if student_code:
            stmt = stmt.where(MealBooking.student_code == student_code)
        rows = db.execute(stmt.order_by(MealBooking.meal, MealBooking.student_code)).scalars().all()
        return [_booking_dict(r) for r in rows]


def meal_booking_delete(booking_id: int) -> bool:
    with session_scope() as db:
        row = db.get(MealBooking, int(booking_id))
        if row is None:
            return False
        db.delete(row)
        return True


def meal_booking_consume(booking_id: int, *, gate_id: Optional[int], ts: Optional[str] = None) -> bool:
    """Mark the booking used; False when it already was (one entry each)."""
    with session_scope() as db:
        row = db.get(MealBooking, int(booking_id))
        if row is None or row.consumed_at:
            return False
        row.consumed_at = ts or _now_iso()
        row.consumed_gate_id = gate_id
        return True


def meal_summary(date: str) -> Dict[str, Dict[str, int]]:
    out = {m: {"booked": 0, "served": 0} for m in MEALS}
    with session_scope() as db:
        rows = db.execute(select(MealBooking.meal, MealBooking.consumed_at).where(MealBooking.date == date)).all()
    for meal, consumed in rows:
        if meal in out:
            out[meal]["booked"] += 1
            if consumed:
                out[meal]["served"] += 1
    return out
