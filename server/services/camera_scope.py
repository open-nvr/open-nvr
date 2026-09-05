# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The one answer to "which cameras may this user see / control?"

The camera routes, streams and recordings each grew their own copy of
the rule — some owner-only, one that also honoured CameraPermission
grants, one that let owner-less cameras through to everyone — and the
newer read surfaces (timeline, plates, occupancy, the alerts inbox, app
state) either copied the owner-only version or scoped nothing at all.
A user GRANTED a camera saw its live stream but none of its plates,
alarms or occupancy; a user granted nothing saw every alarm on the
site. This module is the single rule every surface calls:

* **superuser** — everything.
* **view**  — cameras the user owns, plus cameras with a
  ``CameraPermission.can_view`` grant.
* **manage** — cameras the user owns, plus ``can_manage`` grants.
* **owner-less cameras** (legacy rows, ``owner_id`` NULL) are visible to
  superusers only until an owner or a grant is assigned.
* A soft-deleted (binned) camera stays in the scope of the people who
  could see it: its history, plates and recordings outlive it (the
  Deleted Cameras page relies on this), and the camera routes themselves
  already hide binned rows from the live list.

Everything is expressed as a *set of camera ids* (``None`` meaning
"unrestricted", the superuser case) so callers can filter a query, a
list of rows, or an app's ``/state`` dict with the same value.
"""

from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy.orm import Session

_CAMERA_HANDLE = re.compile(r"^cam-?(\d+)$", re.IGNORECASE)


def camera_id_from_handle(value: object) -> int | None:
    """``"cam3"`` / ``"cam-3"`` / ``"3"`` / ``3`` → ``3``; anything else
    → None. The bus and the apps key cameras by the platform handle,
    the database by the integer id; this is the bridge."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    m = _CAMERA_HANDLE.match(text)
    return int(m.group(1)) if m else None


def _is_superuser(user) -> bool:
    return bool(getattr(user, "is_superuser", False))


def visible_camera_ids(db: Session, user) -> set[int] | None:
    """Ids of every camera ``user`` may SEE — own + can_view grants,
    binned cameras included (their history outlives them) — or ``None``
    for a superuser (no restriction)."""
    if user is None:
        return set()
    if _is_superuser(user):
        return None
    from models import Camera, CameraPermission

    owned = db.query(Camera.id).filter(Camera.owner_id == user.id)
    granted = (
        db.query(Camera.id)
        .join(CameraPermission, CameraPermission.camera_id == Camera.id)
        .filter(CameraPermission.user_id == user.id,
                CameraPermission.can_view == True)  # noqa: E712
    )
    return {row[0] for row in owned.all()} | {row[0] for row in granted.all()}


def manageable_camera_ids(db: Session, user) -> set[int] | None:
    """Ids of every camera ``user`` may CONTROL / configure — own +
    can_manage grants — or ``None`` for a superuser."""
    if user is None:
        return set()
    if _is_superuser(user):
        return None
    from models import Camera, CameraPermission

    owned = db.query(Camera.id).filter(Camera.owner_id == user.id)
    granted = (
        db.query(Camera.id)
        .join(CameraPermission, CameraPermission.camera_id == Camera.id)
        .filter(CameraPermission.user_id == user.id,
                CameraPermission.can_manage == True)  # noqa: E712
    )
    return {row[0] for row in owned.all()} | {row[0] for row in granted.all()}


def can_view_camera(db: Session, user, camera_id: int) -> bool:
    scope = visible_camera_ids(db, user)
    return scope is None or int(camera_id) in scope


def can_manage_camera(db: Session, user, camera_id: int) -> bool:
    scope = manageable_camera_ids(db, user)
    return scope is None or int(camera_id) in scope


def in_scope(scope: set[int] | None, camera: object) -> bool:
    """Is ``camera`` (an id, a handle, or None) inside ``scope``?
    ``None`` scope = unrestricted. A camera that cannot be parsed is
    OUT of scope — an unknown key must never widen what a user sees."""
    if scope is None:
        return True
    cam_id = camera_id_from_handle(camera)
    return cam_id is not None and cam_id in scope


def scope_query(q, camera_column, scope: set[int] | None):
    """Filter a SQLAlchemy query on ``camera_column`` to ``scope``.
    An empty scope matches nothing (never everything)."""
    if scope is None:
        return q
    if not scope:
        return q.filter(camera_column.in_([-1]))
    return q.filter(camera_column.in_(sorted(scope)))


def filter_app_state(state: object, scope: set[int] | None) -> object:
    """Strip an app's ``/state`` payload down to the caller's cameras.

    Apps key per-camera state in two shapes, both handled recursively:
    dicts whose KEYS are camera handles (``{"cam3": {...}}``), lists of
    dicts carrying a ``camera_id`` / ``camera`` / ``id`` handle (the LPR
    review queue, ``per_camera`` tallies) and bare handle lists
    (``"cameras": ["cam1", "cam2"]``). Roll-up numbers computed by the app
    (``total_people``, ``zones_over``…) cannot be re-derived here and
    are left as they are — they are counts, not camera data. ``None``
    scope returns the payload untouched.
    """
    if scope is None:
        return state
    if isinstance(state, dict):
        out = {}
        for key, value in state.items():
            if camera_id_from_handle(key) is not None:
                if in_scope(scope, key):
                    out[key] = filter_app_state(value, scope)
                continue
            out[key] = filter_app_state(value, scope)
        return out
    if isinstance(state, list):
        out_list = []
        for item in state:
            if isinstance(item, dict):
                tag = next((item[k] for k in ("camera_id", "camera", "id")
                            if k in item), None)
                if tag is not None and _is_handle(tag) \
                        and not in_scope(scope, tag):
                    continue
            elif _is_handle(item) and not in_scope(scope, item):
                # A bare roster: ``"cameras": ["cam1", "cam2"]``.
                continue
            out_list.append(filter_app_state(item, scope))
        return out_list
    return state


def _is_handle(value: object) -> bool:
    """A ``camN`` handle specifically — NOT a bare integer or digit
    string, which inside a list or an ``id`` field is as likely to be a
    row id or a count as a camera."""
    return isinstance(value, str) and _CAMERA_HANDLE.match(value.strip()) is not None


def scoped_ids(scope: set[int] | None, candidates: Iterable[object]) -> list[int]:
    """The ids among ``candidates`` (ids or handles) that fall inside
    ``scope`` — for narrowing a caller-supplied camera list."""
    out: list[int] = []
    for c in candidates:
        cam_id = camera_id_from_handle(c)
        if cam_id is not None and (scope is None or cam_id in scope):
            out.append(cam_id)
    return out
