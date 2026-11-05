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

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.database import get_db, release
from core.permissions import RequirePermission
from models import (
    AttendanceAttempt,
    AttendanceAttemptSession,
    AttendanceGroup,
    AttendanceGroupMember,
    AttendanceMark,
    AttendancePerson,
    AttendanceSession,
    AttendanceSighting,
    AttendanceTimetableSlot,
    Camera,
    SecuritySetting,
    User,
)
from routers.internal_camera_agent import _app_roster, _require_internal_key
from services import attendance_faces, attendance_policy, attendance_scheduler
from services.audit_service import audit_request
from services.camera_scope import visible_camera_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attendance", tags=["attendance"])
#: What the attendance app asks core: which sessions run now, on which of
#: its cameras, for whom. App key or site key, never a user JWT.
internal_router = APIRouter(prefix="/internal/app/attendance", tags=["app-platform"])

_view = RequirePermission("attendance.view")
_manage = RequirePermission("attendance.manage")
_enroll = RequirePermission("attendance.enroll")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
#: An end time may also be 24:00, the next midnight: a period that runs to
#: the end of the day (a shift to midnight) has no 23:59 minute missing.
_END_HHMM = re.compile(r"^(([01]\d|2[0-3]):[0-5]\d|24:00)$")
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

    @field_validator("start_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        v = v.strip()
        if not _HHMM.match(v):
            raise ValueError("a start time is HH:MM, 00:00 to 23:59")
        return v

    @field_validator("end_time")
    @classmethod
    def _end_hhmm(cls, v: str) -> str:
        v = v.strip()
        if not _END_HHMM.match(v):
            raise ValueError("an end time is HH:MM, 00:01 to 24:00")
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
        "has_photo": attendance_faces.has_photo(p.uuid),
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
async def remove_person(person_id: int, request: Request, erase: bool = False,
                        db: Session = Depends(get_db), user: User = Depends(_enroll)):
    """Removes a person from attendance. The row stays, so their past marks
    keep a name; they leave every group, so future sessions do not expect
    them, and their reference number is free for somebody else.

    Their faces are forgotten FIRST. If the face service cannot confirm
    that, the removal is refused (503) rather than leaving a biometric
    record behind a person nobody can see any more.

    ``?erase=true`` also erases who they were (an erasure request under
    DPDP/GDPR): name, reference and e-mail are replaced, so their past
    marks remain as counts about "Erased person #id". It works on someone
    already removed too, which is the only way such a row is reached."""
    if erase:
        p = db.get(AttendancePerson, person_id)
        if p is None:
            raise HTTPException(status_code=404, detail="Person not found")
    else:
        p = _person_or_404(db, person_id)
    was_removed = p.deleted_at is not None
    if not was_removed:
        await _forget_faces_or_raise(db, p)
        p = _person_or_404(db, person_id)
        p.deleted_at = datetime.now(UTC)
        p.active = False
        db.query(AttendanceGroupMember).filter(
            AttendanceGroupMember.person_id == p.id).delete(synchronize_session=False)
    if erase:
        p.display_name = f"Erased person #{p.id}"
        p.external_ref = p.email = None
        p.consent_by = None
    db.commit()
    # The audit row of an erasure names nobody: it would be the copy that
    # outlives the erasure.
    audit_request(db, request,
                  action="attendance.person.erase" if erase else "attendance.person.remove",
                  user_id=user.id, entity_type="attendance_person", entity_id=p.id,
                  details=({"already_removed": was_removed} if erase else
                           {"display_name": p.display_name, "external_ref": p.external_ref}))
    return {"removed": p.id, "erased": erase}


# ── Consent and faces ────────────────────────────────────────────────


_FACE_LOCKS: dict[str, asyncio.Lock] = {}


def _face_lock(person_uuid: str) -> asyncio.Lock:
    """One person's face changes, one at a time. The adapter numbers a
    person's samples by position and core keeps a photo beside each, so
    two enrolments (or an enrolment and a delete) of the same person
    interleaving at their awaits would pair photos with the wrong
    samples. Core runs one worker, so a process-local lock is enough."""
    lock = _FACE_LOCKS.get(person_uuid)
    if lock is None:
        lock = _FACE_LOCKS[person_uuid] = asyncio.Lock()
    return lock


#: Photos per enrolment request, and bytes per photo (the adapter's cap).
MAX_ENROL_PHOTOS = 10
MAX_PHOTO_BYTES = 8 * 1024 * 1024


async def _forget_faces_or_raise(db: Session, p: AttendancePerson) -> None:
    """Delete the person's samples in the adapter, releasing the DB
    connection across the call. One the adapter does not know is already
    forgotten.

    The adapter is asked even when core's count says 0: the count is
    written after an enrolment's last photo, so a crash or a removal in
    the middle of one leaves faces the count never saw. Skipped only for
    someone whose consent was never recorded — nothing can have been
    enrolled for them — so removing people works with no face service.
    Their picture goes with their faces, once the adapter has confirmed
    (deleting the photos first and then failing would make "nothing was
    changed" untrue)."""
    uuid = p.uuid
    if p.consent_at is None and not (p.face_samples or 0):
        attendance_faces.drop_photo(uuid)
        return
    release(db)
    try:
        async with _face_lock(uuid):
            await attendance_faces.forget(uuid)
    except attendance_faces.FaceGalleryError as e:
        raise HTTPException(status_code=e.status, detail=(
            f"{e.message} The person's faces could not be deleted, so nothing was changed.")
        ) from None
    attendance_faces.drop_photo(uuid)
    db.query(AttendancePerson).filter(AttendancePerson.uuid == uuid).update(
        {AttendancePerson.face_samples: 0}, synchronize_session=False)
    db.commit()


class ConsentIn(BaseModel):
    given: bool


@router.post("/people/{person_id}/consent")
async def set_consent(person_id: int, payload: ConsentIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(_enroll)):
    """Record that the person (or their guardian) agreed to face
    recognition, or that they withdrew. Withdrawing deletes their faces;
    faces cannot be enrolled without consent on record."""
    p = _person_or_404(db, person_id)
    if payload.given:
        if p.consent_at is None:
            p.consent_at, p.consent_by = datetime.now(UTC), user.id
            db.commit()
    else:
        await _forget_faces_or_raise(db, p)
        p = _person_or_404(db, person_id)
        p.consent_at, p.consent_by = None, None
        db.commit()
    db.refresh(p)
    audit_request(db, request, action="attendance.consent." + ("give" if payload.given else "withdraw"),
                  user_id=user.id, entity_type="attendance_person", entity_id=p.id,
                  details={"display_name": p.display_name})
    return _person_out(p, _groups_of(db, [p.id])[p.id])


@router.post("/people/{person_id}/faces")
async def enrol_faces(person_id: int, request: Request,
                      photos: list[UploadFile] = File(...),
                      db: Session = Depends(get_db), user: User = Depends(_enroll)):
    """Add face photos to a person (1 to 10 per request; several angles and
    lightings recognise better than one). Each photo must hold exactly one
    face. The set is one person: if ANY photo already matches somebody
    else enrolled (the same person registered twice, or the wrong photo
    under this name), none of the set is enrolled. A turned head can fall
    under the duplicate threshold where the straight-on photo of the same
    set did not, so judging photo by photo let the rest through.
    Every photo gets its own result; the ones that pass are enrolled, and
    kept so the person's enrolled photos can be seen and removed."""
    p = _person_or_404(db, person_id)
    if p.consent_at is None:
        raise HTTPException(status_code=409, detail=(
            "Record this person's consent to face recognition before enrolling faces"))
    if not 1 <= len(photos) <= MAX_ENROL_PHOTOS:
        raise HTTPException(status_code=422, detail=f"Send 1 to {MAX_ENROL_PHOTOS} photos")
    images: list[bytes | None] = []
    for up in photos:
        data = await up.read()
        images.append(data if data and len(data) <= MAX_PHOTO_BYTES else None)
    uuid, pid = p.uuid, p.id
    release(db)  # the adapter takes ~0.5 s a photo; hold no connection meanwhile

    # 1. Look at every photo before enrolling any.
    errors: dict[int, dict[str, Any]] = {}
    faces: dict[int, dict[str, Any]] = {}
    duplicate: dict[str, Any] | None = None
    for i, image in enumerate(images):
        if image is None:
            errors[i] = {"error": f"empty or larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB"}
            continue
        try:
            found = await attendance_faces.identify(image)
        except attendance_faces.FaceGalleryError as e:
            if e.status in (502, 503):
                # The service is down: nothing is enrolled yet, and the
                # rest would fail the same way.
                raise HTTPException(status_code=e.status, detail=e.message) from None
            errors[i] = {"error": e.message}
            continue
        n = len(found.get("faces") or [])
        if n != 1:
            errors[i] = {"error": ("no face found" if n == 0 else
                                   f"{n} faces in the photo; enrolment needs exactly one")}
            continue
        dup = attendance_faces.duplicate_of(found, uuid)
        if dup is not None:
            other = db.query(AttendancePerson).filter(
                AttendancePerson.uuid == dup["person_uuid"]).first()
            who = other.display_name if other is not None else "someone else"
            other_id = other.id if other is not None else None
            release(db)
            errors[i] = {"error": (f"this face is already enrolled as {who} "
                                   f"(similarity {dup['similarity']:.2f})"),
                         "duplicate_of": other_id}
            duplicate = duplicate or {"who": who, "index": i, "id": other_id}
            continue
        faces[i] = found["faces"][0]

    if duplicate is not None:
        for i in faces:
            errors[i] = {"error": (
                f"not enrolled: photo {duplicate['index'] + 1} of this set is already "
                f"enrolled as {duplicate['who']}, and one set is one person"),
                "duplicate_of": duplicate["id"]}
        faces = {}

    # 2. Enrol the ones that passed.
    async def gone_meanwhile(registered: bool) -> None:
        # Consent withdrawn or the person removed while this ran (each
        # photo is half a second of adapter time): take back what this
        # request added. Forget already ran for them, and these would
        # otherwise outlive it - for a removed person, behind a row no
        # route can reach any more.
        still = db.query(AttendancePerson.consent_at, AttendancePerson.deleted_at).filter(
            AttendancePerson.id == pid).one()
        release(db)
        if still[0] is not None and still[1] is None:
            return
        if registered:
            try:
                await attendance_faces.forget(uuid)
            except attendance_faces.FaceGalleryError:
                logger.warning("attendance: could not take back faces of person %s", pid)
            attendance_faces.drop_photo(uuid)
            db.query(AttendancePerson).filter(AttendancePerson.id == pid).update(
                {AttendancePerson.face_samples: 0}, synchronize_session=False)
            db.commit()
        raise HTTPException(status_code=409, detail=(
            "Consent was withdrawn or the person was removed during the enrolment; "
            "nothing from it was kept."))

    samples = None
    async with _face_lock(uuid):
        for i in sorted(faces):
            await gone_meanwhile(samples is not None)
            try:
                samples = await attendance_faces.register(uuid, images[i])
            except attendance_faces.FaceGalleryError as e:
                errors[i] = {"error": e.message}
                if e.status in (502, 503):
                    # The service went down: the rest would fail the same way.
                    for j in faces:
                        if j > i:
                            errors.setdefault(j, {"error": e.message})
                    break
                continue
            attendance_faces.keep_photo(uuid, samples, images[i])
        # Once more after the last photo: a withdrawal that landed during
        # it found nothing of this request to forget yet.
        if samples is not None:
            await gone_meanwhile(True)

    results: list[dict[str, Any]] = []
    for i in range(len(images)):
        if i in errors:
            results.append({"index": i, "ok": False, **errors[i]})
        else:
            # Where the face is, so the page can cut the person's picture.
            results.append({"index": i, "ok": True, "bbox": faces[i].get("bbox")})
    enrolled = sum(1 for r in results if r["ok"])
    if samples is not None:
        db.query(AttendancePerson).filter(AttendancePerson.id == pid).update(
            {AttendancePerson.face_samples: samples}, synchronize_session=False)
        db.commit()
    audit_request(db, request, action="attendance.face.enrol", user_id=user.id,
                  entity_type="attendance_person", entity_id=pid,
                  details={"sent": len(images), "enrolled": enrolled,
                           "refused": len(results) - enrolled,
                           "duplicate_of": duplicate["id"] if duplicate else None})
    p = _person_or_404(db, pid)
    return {"person": _person_out(p, _groups_of(db, [pid])[pid]),
            "enrolled": enrolled, "results": results}


@router.get("/people/{person_id}/faces")
async def list_faces(person_id: int, db: Session = Depends(get_db),
                     _user: User = Depends(_enroll)):
    """The person's enrolled photos, oldest first, one per sample the face
    service holds. ``kept`` is false for a sample enrolled before photos
    were kept: it still counts, but there is nothing to show."""
    p = _person_or_404(db, person_id)
    entries = attendance_faces.kept_photos(p.uuid, p.face_samples or 0)
    return {"samples": p.face_samples or 0, "photos": [
        {"index": i, "kept": e is not None,
         "enrolled_at": (datetime.fromtimestamp(e["at"], UTC).isoformat()
                         if e and isinstance(e.get("at"), (int, float)) else None)}
        for i, e in enumerate(entries)]}


@router.get("/people/{person_id}/faces/{index}/image")
async def face_image(person_id: int, index: int, db: Session = Depends(get_db),
                     _user: User = Depends(_enroll)):
    p = _person_or_404(db, person_id)
    path = attendance_faces.kept_photo_file(p.uuid, p.face_samples or 0, index)
    if path is None:
        raise HTTPException(status_code=404, detail="That photo was not kept")
    return Response(path.read_bytes(), media_type="image/jpeg",
                    headers={"cache-control": "private, no-cache"})


@router.delete("/people/{person_id}/faces/{index}")
async def forget_face(person_id: int, index: int, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(_enroll)):
    """Delete one enrolled photo (0 = the oldest): a blurred one, or one
    that turned out to be somebody else. The last one cannot go this way;
    deleting all of a person's photos is ``DELETE /people/{id}/faces``."""
    p = _person_or_404(db, person_id)
    had = p.face_samples or 0
    if not 0 <= index < had:
        raise HTTPException(status_code=404, detail="That photo is no longer enrolled.")
    uuid = p.uuid
    release(db)
    async with _face_lock(uuid):
        try:
            left = await attendance_faces.forget_sample(uuid, index)
        except attendance_faces.FaceGalleryError as e:
            raise HTTPException(status_code=e.status, detail=e.message) from None
        attendance_faces.drop_kept_photo(uuid, had, index)
    db.query(AttendancePerson).filter(AttendancePerson.id == person_id).update(
        {AttendancePerson.face_samples: left}, synchronize_session=False)
    db.commit()
    audit_request(db, request, action="attendance.face.forget_one", user_id=user.id,
                  entity_type="attendance_person", entity_id=person_id,
                  details={"index": index, "samples": left})
    p = _person_or_404(db, person_id)
    return _person_out(p, _groups_of(db, [p.id])[p.id])


@router.delete("/people/{person_id}/faces")
async def forget_faces(person_id: int, request: Request,
                       db: Session = Depends(get_db), user: User = Depends(_enroll)):
    """Delete every face of the person and keep the person (to re-enrol
    after a change of appearance, or because they asked)."""
    p = _person_or_404(db, person_id)
    had = p.face_samples or 0
    if not had:
        # Nothing on record here; ask the adapter anyway, in case a
        # count was lost — forgetting must not depend on core's tally.
        uuid = p.uuid
        release(db)
        try:
            async with _face_lock(uuid):
                await attendance_faces.forget(uuid)
        except attendance_faces.FaceGalleryError as e:
            raise HTTPException(status_code=e.status, detail=e.message) from None
        attendance_faces.drop_photo(uuid)
    else:
        await _forget_faces_or_raise(db, p)
    audit_request(db, request, action="attendance.face.forget", user_id=user.id,
                  entity_type="attendance_person", entity_id=person_id,
                  details={"samples": had})
    p = _person_or_404(db, person_id)
    return _person_out(p, _groups_of(db, [p.id])[p.id])


#: A guidance frame from the enrolling page's webcam, downscaled there.
MAX_CHECK_BYTES = 2 * 1024 * 1024


@router.post("/faces/check")
async def check_face(frame: UploadFile = File(...), db: Session = Depends(get_db),
                     _user: User = Depends(_enroll)):
    """Where the faces are in a webcam frame, so the enrolling page can say
    "come closer" and take the photo itself once one face is well placed
    and turned as asked. Nothing is kept, and who it is never comes back:
    only boxes and landmarks. The
    page sends at most one at a time."""
    data = await frame.read()
    if not data or len(data) > MAX_CHECK_BYTES:
        raise HTTPException(status_code=422, detail=(
            f"Send one frame of up to {MAX_CHECK_BYTES // (1024 * 1024)} MB"))
    release(db)
    try:
        # The detector alone: this runs about once a second per enrolling
        # page, beside the classroom looks on the same adapter.
        found = await attendance_faces.identify(data, match=False)
    except attendance_faces.FaceGalleryError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from None
    return {
        # The landmarks (eyes, nose, mouth corners) say how the head is
        # turned, so the page can check "chin up" was followed.
        "faces": [{"bbox": f.get("bbox"), "confidence": f.get("confidence"),
                   "landmarks": f.get("landmarks")}
                  for f in found.get("faces") or []],
        "too_small": int((found.get("dropped") or {}).get("too_small") or 0),
        "frame": found.get("frame") or {},
    }


@router.get("/people/{person_id}/photo")
async def get_photo(person_id: int, db: Session = Depends(get_db),
                    _user: User = Depends(_view)):
    """The person's picture, cut from one of their enrolled photos."""
    p = _person_or_404(db, person_id)
    path = attendance_faces.photo_path(p.uuid)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="No picture for this person")
    return Response(path.read_bytes(), media_type="image/jpeg",
                    headers={"cache-control": "private, no-cache"})


@router.put("/people/{person_id}/photo")
async def set_photo(person_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(_enroll)):
    """Set the person's picture: a small JPEG (the enrolling page sends a
    face crop of an accepted photo). Only with consent on record; it is
    deleted whenever their faces are."""
    p = _person_or_404(db, person_id)
    if p.consent_at is None:
        raise HTTPException(status_code=409, detail=(
            "Record this person's consent to face recognition before adding a picture"))
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > attendance_faces.MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="The picture is too large")
    data = await request.body()
    try:
        attendance_faces.save_photo(p.uuid, data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    audit_request(db, request, action="attendance.photo.set", user_id=user.id,
                  entity_type="attendance_person", entity_id=p.id,
                  details={"bytes": len(data)})
    return _person_out(p, _groups_of(db, [p.id])[p.id])


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
    # A session not yet started is the slot's future, not its history: it
    # goes with the slot. Unlinked and left scheduled it would run anyway,
    # with nothing to say where it came from.
    db.query(AttendanceSession).filter(
        AttendanceSession.slot_id == slot_id, AttendanceSession.status == "scheduled"
    ).update({AttendanceSession.status: "cancelled"}, synchronize_session=False)
    # The FK says ON DELETE SET NULL, but SQLite only honours it with
    # foreign_keys on; unlinking here keeps the history on every database.
    db.query(AttendanceSession).filter(AttendanceSession.slot_id == slot_id).update(
        {AttendanceSession.slot_id: None}, synchronize_session=False)
    db.delete(s)
    db.commit()
    audit_request(db, request, action="attendance.slot.delete", user_id=user.id,
                  entity_type="attendance_slot", entity_id=slot_id, details=before)
    return {"deleted": slot_id}


# ── Site policy ──────────────────────────────────────────────────────


@router.get("/policy")
async def get_policy(db: Session = Depends(get_db), _user: User = Depends(_view)):
    return {"policy": attendance_policy.site_policy(db),
            "defaults": dict(attendance_policy.DEFAULT_POLICY)}


@router.put("/policy")
async def put_policy(payload: dict[str, Any], request: Request,
                     db: Session = Depends(get_db), user: User = Depends(_manage)):
    """Set any subset of the site policy; keys left out keep their value.
    Every group's overrides must still make sense on top of the result."""
    try:
        changes = attendance_policy.validate_overrides(payload)
        merged = {**attendance_policy.site_policy(db), **changes}
        attendance_policy.check_consistent(merged)
        for g in db.query(AttendanceGroup).filter(AttendanceGroup.policy.isnot(None)).all():
            attendance_policy.check_consistent(
                attendance_policy.effective_policy(merged, g.policy))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"policy: {e}") from None
    row = db.query(SecuritySetting).filter(
        SecuritySetting.key == attendance_policy.SITE_POLICY_KEY).first()
    if row is None:
        db.add(SecuritySetting(key=attendance_policy.SITE_POLICY_KEY, json_value=json.dumps(merged)))
    else:
        row.json_value = json.dumps(merged)
    db.commit()
    audit_request(db, request, action="attendance.policy.update", user_id=user.id,
                  entity_type="attendance_policy", entity_id="site", details=changes)
    return {"policy": merged}


# ── Sessions and marks ───────────────────────────────────────────────


MARK_STATUSES = ("PRESENT", "LATE", "ABSENT", "EXCUSED")


def _session_out(s: AttendanceSession, counts: dict[str, int] | None = None) -> dict:
    return {
        "id": s.id, "group_id": s.group_id, "slot_id": s.slot_id,
        "local_date": _iso(s.local_date), "start_at": _iso(s.start_at),
        "end_at": _iso(s.end_at), "camera_ids": list(s.camera_ids or []),
        "status": s.status, "finalized_at": _iso(s.finalized_at),
        "total_buckets": s.total_buckets, "counted_buckets": s.counted_buckets,
        "no_coverage": bool(s.no_coverage), "counts": counts or {},
    }


def _mark_counts(db: Session, session_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {sid: {} for sid in session_ids}
    if not session_ids:
        return out
    for sid, status, n in (db.query(AttendanceMark.session_id, AttendanceMark.status, func.count())
                           .filter(AttendanceMark.session_id.in_(session_ids))
                           .group_by(AttendanceMark.session_id, AttendanceMark.status).all()):
        out[sid][status] = n
    return out


def _session_or_404(db: Session, session_id: int) -> AttendanceSession:
    s = db.get(AttendanceSession, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


#: Longest range one report covers: a school year, with room to spare.
MAX_REPORT_DAYS = 400


@router.get("/report")
async def report(
    request: Request,
    group_id: int,
    date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
    fmt: str = Query("json", alias="format", pattern="^(json|csv|xlsx)$"),
    db: Session = Depends(get_db),
    user: User = Depends(_view),
):
    """Person × session for one group over a date range (the group's
    local dates): JSON for the page and for partner systems with an
    ``attendance.view`` API token, CSV and XLSX to download. Every export
    is audited, because it is a list of named people and where they were."""
    from services import attendance_report

    g = _group_or_404(db, group_id)
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="'to' is before 'from'")
    if (date_to - date_from).days > MAX_REPORT_DAYS:
        raise HTTPException(status_code=422, detail=f"A report covers at most {MAX_REPORT_DAYS} days")
    body = attendance_report.build(db, g, date_from, date_to)
    audit_request(db, request, action="attendance.report", user_id=getattr(user, "id", None),
                  entity_type="attendance_group", entity_id=g.id,
                  details={"from": body["from"], "to": body["to"], "format": fmt,
                           "people": len(body["people"]), "sessions": len(body["sessions"])})
    if fmt == "json":
        return body
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", g.code or g.name).strip("-") or f"group-{g.id}"
    name = f"attendance-{stem}-{body['from']}-to-{body['to']}"
    if fmt == "csv":
        return Response(content=attendance_report.to_csv(body).encode("utf-8-sig"),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    # A year of a big group is seconds of openpyxl: off the event loop, so
    # every other request does not wait for one export.
    return Response(
        content=await asyncio.to_thread(attendance_report.to_xlsx, body),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})


@router.get("/sessions")
async def list_sessions(
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    group_id: int | None = None,
    status_: str | None = Query(None, alias="status"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    _user: User = Depends(_view),
):
    """Sessions by their local date (the group's), newest first."""
    q = db.query(AttendanceSession)
    if date_from:
        q = q.filter(AttendanceSession.local_date >= date_from)
    if date_to:
        q = q.filter(AttendanceSession.local_date <= date_to)
    if group_id is not None:
        q = q.filter(AttendanceSession.group_id == group_id)
    if status_:
        q = q.filter(AttendanceSession.status == status_)
    rows = q.order_by(AttendanceSession.start_at.desc(), AttendanceSession.id.desc()) \
        .limit(limit).all()
    counts = _mark_counts(db, [s.id for s in rows])
    return {"sessions": [_session_out(s, counts[s.id]) for s in rows]}


@router.get("/sessions/{session_id}")
async def get_session(session_id: int, db: Session = Depends(get_db),
                      _user: User = Depends(_view)):
    """One session: every member's mark (or, before the verdict, who has
    been seen so far), and how much of the session was actually watched."""
    s = _session_or_404(db, session_id)
    members = (db.query(AttendancePerson)
               .join(AttendanceGroupMember, AttendanceGroupMember.person_id == AttendancePerson.id)
               .filter(AttendanceGroupMember.group_id == s.group_id).all())
    marks = {m.person_id: m for m in db.query(AttendanceMark)
             .filter(AttendanceMark.session_id == s.id).all()}
    # People with a mark who have since left the group still belong here.
    member_ids = {m.id for m in members}
    extra = db.query(AttendancePerson).filter(
        AttendancePerson.id.in_([pid for pid in marks if pid not in member_ids])
    ).all() if marks else []
    seen = dict(db.query(AttendanceSighting.person_id, func.count())
                .filter(AttendanceSighting.session_id == s.id)
                .group_by(AttendanceSighting.person_id).all())
    attempts = (db.query(func.count(), func.sum(AttendanceAttempt.faces_detected))
                .join(AttendanceAttemptSession,
                      AttendanceAttemptSession.attempt_id == AttendanceAttempt.id)
                .filter(AttendanceAttemptSession.session_id == s.id,
                        AttendanceAttempt.ok.is_(True)).one())
    people = []
    for p in sorted([*members, *extra], key=lambda p: (p.display_name.lower(), p.id)):
        m = marks.get(p.id)
        people.append({
            "person_id": p.id, "display_name": p.display_name, "external_ref": p.external_ref,
            "status": m.status if m else None, "source": m.source if m else None,
            "buckets_present": m.buckets_present if m else None,
            "buckets_total": m.buckets_total if m else None,
            "first_seen_at": _iso(m.first_seen_at) if m else None,
            "note": m.note if m else None, "sightings": seen.get(p.id, 0),
        })
    out = _session_out(s, _mark_counts(db, [s.id])[s.id])
    out.update({"people": people,
                "attempts_ok": int(attempts[0] or 0), "faces_seen": int(attempts[1] or 0)})
    return out


@router.post("/sessions/{session_id}/refinalize")
async def refinalize(session_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(_manage)):
    """Recompute the automatic marks (after a policy change, or a member
    added late). Manual marks are kept."""
    s = _session_or_404(db, session_id)
    if s.status != "final":
        raise HTTPException(status_code=409, detail="Only a finished session can be re-finalised")
    if attendance_scheduler.evidence_pruned(s):
        raise HTTPException(status_code=409, detail=(
            "This session's camera records have been deleted by retention; its marks are now "
            "the record and cannot be recomputed. Correct individual marks instead."))
    counts = attendance_scheduler.finalize(db, s)
    db.commit()
    audit_request(db, request, action="attendance.session.refinalize", user_id=user.id,
                  entity_type="attendance_session", entity_id=s.id, details=counts)
    return _session_out(s, _mark_counts(db, [s.id])[s.id])


class SessionOffIn(BaseModel):
    #: Why (a holiday, an exam, the room moved): kept in the audit log.
    reason: str | None = Field(None, max_length=200)


@router.post("/sessions/{session_id}/off")
async def call_off_session(session_id: int, request: Request,
                           payload: SessionOffIn | None = None,
                           db: Session = Depends(get_db), user: User = Depends(_manage)):
    """Call one session off: a holiday, an exam, a class that did not
    happen. It is not watched, not marked and not in any report; a
    session already decided keeps its marks, hidden, until it is
    restored. The timetable is untouched, and ``off`` (unlike the
    scheduler's own ``cancelled``) is never revived by it."""
    s = _session_or_404(db, session_id)
    if s.status in ("off", "cancelled"):
        raise HTTPException(status_code=409, detail="This session is not taking place already")
    before = s.status
    s.status = "off"
    db.commit()
    audit_request(db, request, action="attendance.session.off", user_id=user.id,
                  entity_type="attendance_session", entity_id=s.id,
                  details={"from": before, "reason": payload.reason if payload else None})
    return _session_out(s, _mark_counts(db, [s.id])[s.id])


@router.post("/sessions/{session_id}/restore")
async def restore_session(session_id: int, request: Request,
                          db: Session = Depends(get_db), user: User = Depends(_manage)):
    """Undo calling a session off. One that had been decided is final
    again with its marks; any other goes back to wherever its times put
    it now, and the scheduler takes it from there (a session whose end
    has passed is decided from whatever was watched)."""
    s = _session_or_404(db, session_id)
    if s.status != "off":
        raise HTTPException(status_code=409, detail="Only a session that was called off can be restored")
    now = datetime.now(UTC)
    start, end = attendance_scheduler._utc(s.start_at), attendance_scheduler._utc(s.end_at)
    if s.finalized_at is not None:
        s.status = "final"
    elif now < start:
        s.status = "scheduled"
    elif now < end:
        s.status = "running"
    else:
        s.status = "finalizing"
    db.commit()
    audit_request(db, request, action="attendance.session.restore", user_id=user.id,
                  entity_type="attendance_session", entity_id=s.id, details={"to": s.status})
    return _session_out(s, _mark_counts(db, [s.id])[s.id])


class AdHocSessionIn(BaseModel):
    """A one-off session that is not on the timetable: an extra class, a
    make-up lecture, an exam. Times are the group's local wall clock."""
    group_id: int
    local_date: date
    start_time: str
    end_time: str
    camera_ids: list[int] = Field(..., min_length=1, max_length=MAX_SLOT_CAMERAS)

    @field_validator("start_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        v = v.strip()
        if not _HHMM.match(v):
            raise ValueError("a start time is HH:MM, 00:00 to 23:59")
        return v

    @field_validator("end_time")
    @classmethod
    def _end_hhmm(cls, v: str) -> str:
        v = v.strip()
        if not _END_HHMM.match(v):
            raise ValueError("an end time is HH:MM, 00:01 to 24:00")
        return v

    @field_validator("camera_ids")
    @classmethod
    def _dedupe(cls, v: list[int]) -> list[int]:
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _order(self) -> AdHocSessionIn:
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


@router.post("/sessions", status_code=201)
async def create_session(payload: AdHocSessionIn, request: Request,
                         db: Session = Depends(get_db), user: User = Depends(_manage)):
    """Add a one-off session. It must not have ended yet: a session
    nobody watched could only ever be "no coverage"."""
    g = _group_or_404(db, payload.group_id)
    tz = ZoneInfo(g.timezone)
    start, end = attendance_scheduler._local_window(
        payload.local_date, payload.start_time, payload.end_time, tz)
    if end <= start:
        raise HTTPException(status_code=422, detail="Those times do not exist on that day (a clock change)")
    if end <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="That session has already ended")
    s = AttendanceSession(slot_id=None, group_id=g.id, local_date=payload.local_date,
                          start_at=start, end_at=end,
                          camera_ids=_checked_cameras(db, user, payload.camera_ids),
                          status="scheduled")
    db.add(s)
    db.commit()
    db.refresh(s)
    audit_request(db, request, action="attendance.session.create", user_id=user.id,
                  entity_type="attendance_session", entity_id=s.id, details=_session_out(s))
    return _session_out(s)


class MarkIn(BaseModel):
    status: str
    note: str | None = Field(None, max_length=500)

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in (*MARK_STATUSES, "AUTO"):
            raise ValueError(f"status is one of {', '.join(MARK_STATUSES)} or AUTO")
        return v


@router.patch("/sessions/{session_id}/marks/{person_id}")
async def set_mark(session_id: int, person_id: int, payload: MarkIn, request: Request,
                   db: Session = Depends(get_db), user: User = Depends(_manage)):
    """A person's correction. It wins over the cameras and survives
    re-finalising; ``AUTO`` hands the mark back to the cameras."""
    s = _session_or_404(db, session_id)
    if s.status != "final":
        raise HTTPException(status_code=409, detail="Marks can be corrected once the session is final")
    is_member = db.query(AttendanceGroupMember).filter(
        AttendanceGroupMember.group_id == s.group_id,
        AttendanceGroupMember.person_id == person_id).first() is not None
    mark = db.query(AttendanceMark).filter(AttendanceMark.session_id == s.id,
                                           AttendanceMark.person_id == person_id).first()
    if mark is None and not is_member:
        raise HTTPException(status_code=404, detail="Not part of this session")
    before = (mark.status, mark.source) if mark else (None, None)
    if payload.status == "AUTO":
        if attendance_scheduler.evidence_pruned(s):
            raise HTTPException(status_code=409, detail=(
                "This session's camera records have been deleted by retention, so there is "
                "nothing to hand the mark back to. Set the mark by hand instead."))
        if mark is not None:
            mark.source, mark.overridden_by, mark.note = "auto", None, None
        # This one person only: the rest of a past session is not re-marked
        # against today's roster.
        attendance_scheduler.finalize(db, s, only_person=person_id)
    else:
        if mark is None:
            mark = AttendanceMark(session_id=s.id, person_id=person_id)
            db.add(mark)
        mark.status, mark.source = payload.status, "manual"
        mark.overridden_by, mark.note = user.id, payload.note
    db.commit()
    mark = db.query(AttendanceMark).filter(AttendanceMark.session_id == s.id,
                                           AttendanceMark.person_id == person_id).first()
    audit_request(db, request, action="attendance.mark.update", user_id=user.id,
                  entity_type="attendance_session", entity_id=s.id,
                  details={"person_id": person_id, "from": before,
                           "to": (mark.status, mark.source) if mark else None,
                           "note": payload.note})
    return {"session_id": s.id, "person_id": person_id,
            "status": mark.status if mark else None, "source": mark.source if mark else None}


# ── For the attendance app ───────────────────────────────────────────


@internal_router.get("/active")
async def active_sessions(db: Session = Depends(get_db), principal=Depends(_require_internal_key)):
    """Sessions running now on the calling app's cameras: what to look
    at, how often, and whom to look for (gallery ids of the group's
    members). Read by time, not only by status, so a session starts
    being watched at its bell even between two scheduler ticks."""
    now = datetime.now(UTC)
    roster = _app_roster(db, principal)
    rows = (db.query(AttendanceSession)
            .filter(AttendanceSession.status.in_(("scheduled", "running"))).all())
    out = []
    for s in rows:
        start, end = attendance_scheduler._utc(s.start_at), attendance_scheduler._utc(s.end_at)
        if not (start <= now < end):
            continue
        cams = [c for c in (s.camera_ids or []) if roster is None or c in roster]
        if not cams:
            continue
        policy = attendance_scheduler.session_policy(db, s)
        uuids = [u for (u,) in db.query(AttendancePerson.uuid)
                 .join(AttendanceGroupMember, AttendanceGroupMember.person_id == AttendancePerson.id)
                 .filter(AttendanceGroupMember.group_id == s.group_id,
                         AttendancePerson.deleted_at.is_(None),
                         AttendancePerson.active.is_(True)).all()]
        out.append({"session_id": s.id, "group_id": s.group_id, "camera_ids": cams,
                    "start_at": start.isoformat(), "end_at": end.isoformat(),
                    "person_ids": uuids,
                    "attempt_interval_s": policy["attempt_interval_s"],
                    "bucket_minutes": policy["bucket_minutes"],
                    "match_threshold": policy["match_threshold"]})
    return {"now": now.isoformat(), "sessions": out}
