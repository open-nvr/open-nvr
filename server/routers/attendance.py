# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Attendance: the people it is taken for, their groups, and the timetable.

Who was in the room, from the cameras. This router is the roster and the
timetable; the attendance app reports what each frame showed, and core
turns the timetable into sessions and the sightings into marks.

Permissions:

* ``attendance.view`` — read everything here;
* ``attendance.manage`` — groups, members and the timetable;
* ``attendance.enroll`` — the people themselves. Separate from manage
  because it is the one that leads to biometric data.

Every group is visible to everyone with ``attendance.view``; a timetable
slot may only name cameras its editor can see. Removing a person keeps the
row (past marks must still say who they were about) and frees their
reference number for somebody new.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.database import get_db
from core.permissions import RequirePermission
from models import (
    AttendanceGroup,
    AttendanceGroupMember,
    AttendancePerson,
    AttendanceSession,
    AttendanceTimetableSlot,
    Camera,
    User,
)
from services import attendance_policy
from services.audit_service import audit_request
from services.camera_scope import visible_camera_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attendance", tags=["attendance"])

_view = RequirePermission("attendance.view")
_manage = RequirePermission("attendance.manage")
_enroll = RequirePermission("attendance.enroll")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
#: Cameras one slot may watch. A room is a handful of cameras, not a site.
MAX_SLOT_CAMERAS = 8


def _blank_to_none(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


# ── Schemas ──────────────────────────────────────────────────────────


class PersonIn(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)
    #: Roll number, USN, employee id — unique among people not removed.
    external_ref: str | None = Field(None, max_length=64)
    email: str | None = Field(None, max_length=255)
    active: bool = True

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("display_name must not be blank")
        return v

    @field_validator("external_ref", "email")
    @classmethod
    def _optional(cls, v: str | None) -> str | None:
        return _blank_to_none(v)


class PersonPatch(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=200)
    external_ref: str | None = Field(None, max_length=64)
    email: str | None = Field(None, max_length=255)
    active: bool | None = None

    @field_validator("display_name", "external_ref", "email")
    @classmethod
    def _optional(cls, v: str | None) -> str | None:
        return _blank_to_none(v)


class GroupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str | None = Field(None, max_length=40)
    kind_label: str | None = Field(None, max_length=40)
    timezone: str = Field(..., min_length=1, max_length=64)
    policy: dict[str, Any] | None = None
    active: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("code", "kind_label")
    @classmethod
    def _optional(cls, v: str | None) -> str | None:
        return _blank_to_none(v)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _valid_timezone(v)


class GroupPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    code: str | None = Field(None, max_length=40)
    kind_label: str | None = Field(None, max_length=40)
    timezone: str | None = Field(None, min_length=1, max_length=64)
    policy: dict[str, Any] | None = None
    active: bool | None = None

    @field_validator("code", "kind_label")
    @classmethod
    def _optional(cls, v: str | None) -> str | None:
        return _blank_to_none(v)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else _valid_timezone(v)


class MembersIn(BaseModel):
    person_ids: list[int] = Field(..., max_length=5000)


class SlotIn(BaseModel):
    group_id: int
    weekday: int = Field(..., ge=0, le=6)
    start_time: str
    end_time: str
    camera_ids: list[int] = Field(..., min_length=1, max_length=MAX_SLOT_CAMERAS)
    valid_from: date | None = None
    valid_to: date | None = None
    active: bool = True

    @field_validator("start_time", "end_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        v = v.strip()
        if not _HHMM.match(v):
            raise ValueError("times are HH:MM, 00:00 to 23:59")
        return v

    @field_validator("camera_ids")
    @classmethod
    def _dedupe(cls, v: list[int]) -> list[int]:
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _order(self) -> SlotIn:
        # No period runs past midnight: a night shift is two slots.
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")
        return self


def _valid_timezone(v: str) -> str:
    v = v.strip()
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {v!r} (an IANA name, e.g. Asia/Kolkata)") from None
    return v


# ── Serialisers and lookups ──────────────────────────────────────────


def _iso(dt: datetime | date | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _person_out(p: AttendancePerson, group_ids: list[int]) -> dict:
    return {
        "id": p.id, "uuid": p.uuid, "display_name": p.display_name,
        "external_ref": p.external_ref, "email": p.email, "active": bool(p.active),
        "consent_at": _iso(p.consent_at), "face_samples": p.face_samples or 0,
        "group_ids": group_ids, "created_at": _iso(p.created_at),
        "removed_at": _iso(p.deleted_at),
    }


def _group_out(g: AttendanceGroup, members: int) -> dict:
    return {
        "id": g.id, "name": g.name, "code": g.code, "kind_label": g.kind_label,
        "timezone": g.timezone, "policy": g.policy or {}, "active": bool(g.active),
        "members": members, "created_at": _iso(g.created_at),
    }


def _slot_out(s: AttendanceTimetableSlot) -> dict:
    return {
        "id": s.id, "group_id": s.group_id, "weekday": s.weekday,
        "start_time": s.start_time, "end_time": s.end_time,
        "camera_ids": list(s.camera_ids or []),
        "valid_from": _iso(s.valid_from), "valid_to": _iso(s.valid_to),
        "active": bool(s.active),
    }


def _groups_of(db: Session, person_ids: list[int]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {pid: [] for pid in person_ids}
    if not person_ids:
        return out
    rows = (db.query(AttendanceGroupMember.person_id, AttendanceGroupMember.group_id)
            .filter(AttendanceGroupMember.person_id.in_(person_ids))
            .order_by(AttendanceGroupMember.group_id).all())
    for pid, gid in rows:
        out[pid].append(gid)
    return out


def _member_counts(db: Session, group_ids: list[int]) -> dict[int, int]:
    if not group_ids:
        return {}
    rows = (db.query(AttendanceGroupMember.group_id, func.count())
            .join(AttendancePerson, AttendancePerson.id == AttendanceGroupMember.person_id)
            .filter(AttendanceGroupMember.group_id.in_(group_ids),
                    AttendancePerson.deleted_at.is_(None))
            .group_by(AttendanceGroupMember.group_id).all())
    return dict(rows)


def _person_or_404(db: Session, person_id: int) -> AttendancePerson:
    p = (db.query(AttendancePerson)
         .filter(AttendancePerson.id == person_id, AttendancePerson.deleted_at.is_(None))
         .first())
    if p is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return p


def _group_or_404(db: Session, group_id: int) -> AttendanceGroup:
    g = db.query(AttendanceGroup).filter(AttendanceGroup.id == group_id).first()
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return g


def _slot_or_404(db: Session, slot_id: int) -> AttendanceTimetableSlot:
    s = db.query(AttendanceTimetableSlot).filter(AttendanceTimetableSlot.id == slot_id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="Timetable slot not found")
    return s


def _ref_taken(db: Session, ref: str | None, *, except_id: int | None = None) -> bool:
    if not ref:
        return False
    q = db.query(AttendancePerson.id).filter(
        AttendancePerson.external_ref == ref, AttendancePerson.deleted_at.is_(None))
    if except_id is not None:
        q = q.filter(AttendancePerson.id != except_id)
    return q.first() is not None


def _code_taken(db: Session, code: str | None, *, except_id: int | None = None) -> bool:
    if not code:
        return False
    q = db.query(AttendanceGroup.id).filter(AttendanceGroup.code == code)
    if except_id is not None:
        q = q.filter(AttendanceGroup.id != except_id)
    return q.first() is not None


def _checked_policy(db: Session, overrides: dict[str, Any] | None) -> dict[str, Any] | None:
    """The group's overrides, validated alone and as part of the site
    policy they will sit on; 422 naming what is wrong."""
    try:
        clean = attendance_policy.validate_overrides(overrides)
        attendance_policy.check_consistent(
            attendance_policy.effective_policy(attendance_policy.site_policy(db), clean))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"policy: {e}") from None
    return clean or None


def _checked_cameras(db: Session, user: User, camera_ids: list[int]) -> list[int]:
    """A slot names only cameras that exist, are not in the bin, and that
    the editor can see: a timetable must not be a way to point attendance
    at a camera you have no business looking through."""
    found = {cid for (cid,) in db.query(Camera.id).filter(
        Camera.id.in_(camera_ids), Camera.deleted_at.is_(None)).all()}
    scope = visible_camera_ids(db, user)
    bad = [c for c in camera_ids if c not in found or (scope is not None and c not in scope)]
    if bad:
        raise HTTPException(status_code=422, detail=f"unknown camera(s): {bad}")
    return camera_ids


# ── People ───────────────────────────────────────────────────────────


@router.get("/people")
async def list_people(
    q: str | None = Query(None, max_length=100),
    group_id: int | None = None,
    include_removed: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(_view),
):
    query = db.query(AttendancePerson)
    if not include_removed:
        query = query.filter(AttendancePerson.deleted_at.is_(None))
    if group_id is not None:
        query = query.join(AttendanceGroupMember,
                           AttendanceGroupMember.person_id == AttendancePerson.id
                           ).filter(AttendanceGroupMember.group_id == group_id)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.filter(or_(func.lower(AttendancePerson.display_name).like(like),
                                 func.lower(AttendancePerson.external_ref).like(like),
                                 func.lower(AttendancePerson.email).like(like)))
    total = query.count()
    rows = (query.order_by(func.lower(AttendancePerson.display_name), AttendancePerson.id)
            .offset(offset).limit(limit).all())
    groups = _groups_of(db, [p.id for p in rows])
    return {"people": [_person_out(p, groups[p.id]) for p in rows], "total": total}


@router.get("/people/{person_id}")
async def get_person(person_id: int, db: Session = Depends(get_db),
                     _user: User = Depends(_view)):
    p = _person_or_404(db, person_id)
    return _person_out(p, _groups_of(db, [p.id])[p.id])


@router.post("/people", status_code=201)
async def create_person(payload: PersonIn, request: Request,
                        db: Session = Depends(get_db), user: User = Depends(_enroll)):
    if _ref_taken(db, payload.external_ref):
        raise HTTPException(status_code=409, detail="That reference number is already in use")
    p = AttendancePerson(display_name=payload.display_name, external_ref=payload.external_ref,
                         email=payload.email, active=payload.active)
    db.add(p)
    db.commit()
    db.refresh(p)
    audit_request(db, request, action="attendance.person.create", user_id=user.id,
                  entity_type="attendance_person", entity_id=p.id,
                  details={"display_name": p.display_name, "external_ref": p.external_ref})
    return _person_out(p, [])


@router.patch("/people/{person_id}")
async def update_person(person_id: int, payload: PersonPatch, request: Request,
                        db: Session = Depends(get_db), user: User = Depends(_enroll)):
    p = _person_or_404(db, person_id)
    changes = payload.model_dump(exclude_unset=True)
    if "display_name" in changes and not changes["display_name"]:
        raise HTTPException(status_code=422, detail="display_name must not be blank")
    if "external_ref" in changes and _ref_taken(db, changes["external_ref"], except_id=p.id):
        raise HTTPException(status_code=409, detail="That reference number is already in use")
    for field, value in changes.items():
        setattr(p, field, value)
    db.commit()
    db.refresh(p)
    audit_request(db, request, action="attendance.person.update", user_id=user.id,
                  entity_type="attendance_person", entity_id=p.id,
                  details={"changed": sorted(changes)})
    return _person_out(p, _groups_of(db, [p.id])[p.id])


@router.delete("/people/{person_id}")
async def remove_person(person_id: int, request: Request,
                        db: Session = Depends(get_db), user: User = Depends(_enroll)):
    """Removes a person from attendance. The row stays, so their past marks
    keep a name; they leave every group, so future sessions do not expect
    them, and their reference number is free for somebody else."""
    p = _person_or_404(db, person_id)
    p.deleted_at = datetime.now(UTC)
    p.active = False
    db.query(AttendanceGroupMember).filter(
        AttendanceGroupMember.person_id == p.id).delete(synchronize_session=False)
    db.commit()
    audit_request(db, request, action="attendance.person.remove", user_id=user.id,
                  entity_type="attendance_person", entity_id=p.id,
                  details={"display_name": p.display_name, "external_ref": p.external_ref})
    return {"removed": p.id}


# ── Groups and members ───────────────────────────────────────────────


@router.get("/groups")
async def list_groups(include_inactive: bool = True, db: Session = Depends(get_db),
                      _user: User = Depends(_view)):
    query = db.query(AttendanceGroup)
    if not include_inactive:
        query = query.filter(AttendanceGroup.active.is_(True))
    rows = query.order_by(func.lower(AttendanceGroup.name), AttendanceGroup.id).all()
    counts = _member_counts(db, [g.id for g in rows])
    return {"groups": [_group_out(g, counts.get(g.id, 0)) for g in rows]}


@router.get("/groups/{group_id}")
async def get_group(group_id: int, db: Session = Depends(get_db), _user: User = Depends(_view)):
    g = _group_or_404(db, group_id)
    return _group_out(g, _member_counts(db, [g.id]).get(g.id, 0))


@router.post("/groups", status_code=201)
async def create_group(payload: GroupIn, request: Request,
                       db: Session = Depends(get_db), user: User = Depends(_manage)):
    if _code_taken(db, payload.code):
        raise HTTPException(status_code=409, detail="That group code is already in use")
    g = AttendanceGroup(name=payload.name, code=payload.code, kind_label=payload.kind_label,
                        timezone=payload.timezone, policy=_checked_policy(db, payload.policy),
                        active=payload.active)
    db.add(g)
    db.commit()
    db.refresh(g)
    audit_request(db, request, action="attendance.group.create", user_id=user.id,
                  entity_type="attendance_group", entity_id=g.id, details={"name": g.name})
    return _group_out(g, 0)


@router.patch("/groups/{group_id}")
async def update_group(group_id: int, payload: GroupPatch, request: Request,
                       db: Session = Depends(get_db), user: User = Depends(_manage)):
    g = _group_or_404(db, group_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        if not (changes["name"] or "").strip():
            raise HTTPException(status_code=422, detail="name must not be blank")
        changes["name"] = changes["name"].strip()
    if "code" in changes and _code_taken(db, changes["code"], except_id=g.id):
        raise HTTPException(status_code=409, detail="That group code is already in use")
    if "timezone" in changes and changes["timezone"] is None:
        raise HTTPException(status_code=422, detail="timezone must not be empty")
    if "policy" in changes:
        changes["policy"] = _checked_policy(db, changes["policy"])
    for field, value in changes.items():
        setattr(g, field, value)
    db.commit()
    db.refresh(g)
    audit_request(db, request, action="attendance.group.update", user_id=user.id,
                  entity_type="attendance_group", entity_id=g.id,
                  details={"changed": sorted(changes)})
    return _group_out(g, _member_counts(db, [g.id]).get(g.id, 0))


@router.delete("/groups/{group_id}")
async def delete_group(group_id: int, request: Request,
                       db: Session = Depends(get_db), user: User = Depends(_manage)):
    """Only a group that never had a session can be deleted: deleting one
    with history would delete its marks with it. Deactivate it instead."""
    g = _group_or_404(db, group_id)
    if db.query(AttendanceSession.id).filter(AttendanceSession.group_id == g.id).first():
        raise HTTPException(status_code=409, detail=(
            "This group has attendance history; deactivate it instead of deleting it"))
    name = g.name
    db.delete(g)
    db.commit()
    audit_request(db, request, action="attendance.group.delete", user_id=user.id,
                  entity_type="attendance_group", entity_id=group_id, details={"name": name})
    return {"deleted": group_id}


def _live_people(db: Session, person_ids: list[int]) -> list[int]:
    ids = list(dict.fromkeys(person_ids))
    found = {pid for (pid,) in db.query(AttendancePerson.id).filter(
        AttendancePerson.id.in_(ids), AttendancePerson.deleted_at.is_(None)).all()}
    missing = [pid for pid in ids if pid not in found]
    if missing:
        raise HTTPException(status_code=422, detail=f"unknown person id(s): {missing[:20]}")
    return ids


@router.get("/groups/{group_id}/members")
async def list_members(group_id: int, db: Session = Depends(get_db),
                       _user: User = Depends(_view)):
    _group_or_404(db, group_id)
    rows = (db.query(AttendancePerson)
            .join(AttendanceGroupMember, AttendanceGroupMember.person_id == AttendancePerson.id)
            .filter(AttendanceGroupMember.group_id == group_id,
                    AttendancePerson.deleted_at.is_(None))
            .order_by(func.lower(AttendancePerson.display_name), AttendancePerson.id).all())
    groups = _groups_of(db, [p.id for p in rows])
    return {"people": [_person_out(p, groups[p.id]) for p in rows]}


@router.put("/groups/{group_id}/members")
async def set_members(group_id: int, payload: MembersIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(_manage)):
    """Replace the group's members with exactly this list."""
    _group_or_404(db, group_id)
    wanted = set(_live_people(db, payload.person_ids))
    current = {pid for (pid,) in db.query(AttendanceGroupMember.person_id).filter(
        AttendanceGroupMember.group_id == group_id).all()}
    if current - wanted:
        db.query(AttendanceGroupMember).filter(
            AttendanceGroupMember.group_id == group_id,
            AttendanceGroupMember.person_id.in_(current - wanted)).delete(synchronize_session=False)
    db.add_all(AttendanceGroupMember(group_id=group_id, person_id=pid)
               for pid in wanted - current)
    db.commit()
    audit_request(db, request, action="attendance.group.members", user_id=user.id,
                  entity_type="attendance_group", entity_id=group_id,
                  details={"added": len(wanted - current), "removed": len(current - wanted)})
    return {"group_id": group_id, "members": len(wanted)}


@router.post("/groups/{group_id}/members")
async def add_members(group_id: int, payload: MembersIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(_manage)):
    _group_or_404(db, group_id)
    ids = _live_people(db, payload.person_ids)
    current = {pid for (pid,) in db.query(AttendanceGroupMember.person_id).filter(
        AttendanceGroupMember.group_id == group_id).all()}
    new = [pid for pid in ids if pid not in current]
    db.add_all(AttendanceGroupMember(group_id=group_id, person_id=pid) for pid in new)
    db.commit()
    audit_request(db, request, action="attendance.group.members", user_id=user.id,
                  entity_type="attendance_group", entity_id=group_id,
                  details={"added": len(new), "removed": 0})
    return {"group_id": group_id, "added": len(new)}


@router.delete("/groups/{group_id}/members/{person_id}")
async def remove_member(group_id: int, person_id: int, request: Request,
                        db: Session = Depends(get_db), user: User = Depends(_manage)):
    _group_or_404(db, group_id)
    n = db.query(AttendanceGroupMember).filter(
        AttendanceGroupMember.group_id == group_id,
        AttendanceGroupMember.person_id == person_id).delete(synchronize_session=False)
    db.commit()
    if not n:
        raise HTTPException(status_code=404, detail="Not a member of this group")
    audit_request(db, request, action="attendance.group.members", user_id=user.id,
                  entity_type="attendance_group", entity_id=group_id,
                  details={"added": 0, "removed": 1})
    return {"group_id": group_id, "removed": person_id}


# ── Timetable ────────────────────────────────────────────────────────


@router.get("/timetable")
async def list_slots(group_id: int | None = None, db: Session = Depends(get_db),
                     _user: User = Depends(_view)):
    query = db.query(AttendanceTimetableSlot)
    if group_id is not None:
        query = query.filter(AttendanceTimetableSlot.group_id == group_id)
    rows = query.order_by(AttendanceTimetableSlot.weekday, AttendanceTimetableSlot.start_time,
                          AttendanceTimetableSlot.id).all()
    return {"slots": [_slot_out(s) for s in rows]}


@router.post("/timetable", status_code=201)
async def create_slot(payload: SlotIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(_manage)):
    _group_or_404(db, payload.group_id)
    s = AttendanceTimetableSlot(
        group_id=payload.group_id, weekday=payload.weekday, start_time=payload.start_time,
        end_time=payload.end_time, camera_ids=_checked_cameras(db, user, payload.camera_ids),
        valid_from=payload.valid_from, valid_to=payload.valid_to, active=payload.active)
    db.add(s)
    db.commit()
    db.refresh(s)
    audit_request(db, request, action="attendance.slot.create", user_id=user.id,
                  entity_type="attendance_slot", entity_id=s.id, details=_slot_out(s))
    return _slot_out(s)


@router.put("/timetable/{slot_id}")
async def update_slot(slot_id: int, payload: SlotIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(_manage)):
    """Replace a slot. Sessions already taken from it keep their own times
    and cameras; sessions not yet started follow the change (the scheduler
    re-reads the slot before a session starts)."""
    s = _slot_or_404(db, slot_id)
    _group_or_404(db, payload.group_id)
    s.group_id, s.weekday = payload.group_id, payload.weekday
    s.start_time, s.end_time = payload.start_time, payload.end_time
    s.camera_ids = _checked_cameras(db, user, payload.camera_ids)
    s.valid_from, s.valid_to, s.active = payload.valid_from, payload.valid_to, payload.active
    db.commit()
    db.refresh(s)
    audit_request(db, request, action="attendance.slot.update", user_id=user.id,
                  entity_type="attendance_slot", entity_id=s.id, details=_slot_out(s))
    return _slot_out(s)


@router.delete("/timetable/{slot_id}")
async def delete_slot(slot_id: int, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(_manage)):
    """Sessions already taken from the slot stay, with their marks."""
    s = _slot_or_404(db, slot_id)
    before = _slot_out(s)
    # The FK says ON DELETE SET NULL, but SQLite only honours it with
    # foreign_keys on; unlinking here keeps the history on every database.
    db.query(AttendanceSession).filter(AttendanceSession.slot_id == slot_id).update(
        {AttendanceSession.slot_id: None}, synchronize_session=False)
    db.delete(s)
    db.commit()
    audit_request(db, request, action="attendance.slot.delete", user_id=user.id,
                  entity_type="attendance_slot", entity_id=slot_id, details=before)
    return {"deleted": slot_id}
