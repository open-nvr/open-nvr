# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Live View tours: camera sets a display board cycles through.

A tour is ordered pages. A page is one screenful: a layout, the camera on
each of its tiles and how long to show them. Running a tour needs
``live.view``; changing one needs ``cameras.manage`` (the camera-zones rule)
and the right to manage every camera still in it.

A page never holds more cameras than its layout has tiles. One that does —
a tour saved when a step paged through more cameras than its layout held,
a custom layout made smaller later, or an older client — is split into
consecutive pages of the same label, layout and time, exactly as the wall
showed it, both when it is saved and when it is read.

Every read is filtered to the caller: a page only ever lists cameras the
caller may see and that are not in the bin, plus a count of the rest, so a
tour never leaks a camera and survives a camera being deleted. A layout the
tour names may later be deleted or disabled in Window Settings; the read
says so (``layout_missing``) and the player falls back to an automatic grid.
A page may also name a plain grid, ``grid:<cols>x<rows>`` (1..8 each),
which needs no Window Settings entry.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import AliasChoices, BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.camera_source import is_publisher
from core.database import get_db, release
from core.permissions import RequirePermission
from models import Camera, CameraTour, User
from schemas import WindowDivisionSettings
from services import camera_thumbnail as camera_thumbnail_service
from services.audit_service import audit_request
from services.camera_scope import can_view_camera, manageable_camera_ids, visible_camera_ids

logger = logging.getLogger(__name__)

#: A tour has no page limit: a wall only ever plays the page on screen (and
#: the next, for its last seconds), so how many pages it has costs the
#: display and the recorder nothing. This only stops a runaway request
#: (nginx takes bodies up to 256 MB, for uploads) from making core parse
#: it: at ~100 bytes a page it is some 50,000 pages, far past any tour.
MAX_BODY_BYTES = 5 * 1024 * 1024


class _CappedRoute(APIRoute):
    """Refuses a body over MAX_BODY_BYTES on its declared length, before
    FastAPI reads and parses it (a dependency would run too late)."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def capped(request: Request) -> Response:
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="This tour is too large to save.")
            return await handler(request)

        return capped


router = APIRouter(prefix="/tours", tags=["tours"], route_class=_CappedRoute)

_view = RequirePermission("live.view")
_manage = RequirePermission("cameras.manage")

#: More than any layout has tiles (8x8): a longer list is split into pages.
MAX_CAMERAS_PER_PAGE = 256
# A plain C×R grid needs no Window Settings entry: "grid:5x4" is the
# 20-camera wall without first building a 20-tile custom layout by hand.
_GRID_RE = re.compile(r"^grid:([1-8])x([1-8])$")
# Tiles of Live View's built-in layouts (app/src/lib/liveLayouts.ts).
_PREDEFINED_TILES = {"1x1": 1, "2x2": 4, "3x3": 9, "4x4": 16, "1+5": 6, "1+7": 8,
                     "2+8": 10, "1+12": 13, "4+9": 13, "1+1+10": 12}
# A page whose layout is gone falls back to an automatic grid that never
# grows past 5x5 (tourPlan.ts AUTO_GRID_MAX), so that is its page size.
_AUTO_GRID_MAX = 25


def _trim(ids: list[int | None]) -> list[int | None]:
    """Without the empty tiles at the end: they are simply where the page stops."""
    end = len(ids)
    while end and ids[end - 1] is None:
        end -= 1
    return ids[:end]


class TourPageIn(BaseModel):
    label: str = Field("", max_length=60)
    layout_id: str = Field(..., min_length=1, max_length=50)
    duration_s: int = Field(60, ge=10, le=3600)
    #: In tile order; ``null`` is a tile left empty, so taking a camera out
    #: of a wall moves no other camera.
    camera_ids: list[int | None] = Field(..., min_length=1, max_length=MAX_CAMERAS_PER_PAGE)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return v.strip()

    @field_validator("camera_ids")
    @classmethod
    def _dedupe(cls, v: list[int | None]) -> list[int | None]:
        # Keep the admin's order and every camera's tile; a camera twice in
        # one page is a slip, and its second tile is left empty.
        seen: set[int] = set()
        out: list[int | None] = []
        for c in v:
            out.append(None if c is None or c in seen else c)
            if c is not None:
                seen.add(c)
        out = _trim(out)
        if not out:
            raise ValueError("a page needs at least one camera")
        return out


class TourAlerts(BaseModel):
    """Alarm takeover: an alert on a camera brings it to the front of the
    wall for ``hold_s``, then the tour carries on where it was."""

    enabled: bool = False
    #: app_alert = alerts from apps and camera analytics (alerts inbox);
    #: camera_event = the camera's own alarms (motion, tamper, line crossing).
    sources: list[Literal["app_alert", "camera_event"]] = Field(
        default_factory=lambda: ["app_alert", "camera_event"], min_length=1)
    #: App alerts below this severity are ignored (alerts_inbox._SEVERITIES).
    min_severity: Literal["low", "medium", "high", "critical"] = "medium"
    #: "tour" = only cameras in this tour; "visible" = any camera the viewer sees.
    scope: Literal["tour", "visible"] = "tour"
    hold_s: int = Field(30, ge=5, le=600)
    #: A camera that just took over cannot take over again for this long.
    cooldown_s: int = Field(60, ge=0, le=3600)

    @field_validator("sources")
    @classmethod
    def _dedupe_sources(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))


class TourSettings(BaseModel):
    #: "fit" letterboxes each camera; "fill" crops it to fill its tile.
    fit: Literal["fit", "fill"] = "fit"
    #: How one page gives way to the next: a straight cut or a short crossfade.
    transition: Literal["cut", "fade"] = "fade"
    #: Play the next page's cameras, hidden, for the last seconds of a page,
    #: so they are already playing when it shows.
    preload: bool = True
    alerts: TourAlerts = Field(default_factory=TourAlerts)


def _settings_of(tour: CameraTour) -> dict:
    """The tour's settings over the defaults; a stored value that no longer
    validates falls back to the defaults rather than failing the read."""
    try:
        return TourSettings(**(tour.settings or {})).model_dump()
    except Exception:
        logger.warning("tour %s has unreadable settings; using defaults", tour.id)
        return TourSettings().model_dump()


class TourIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    enabled: bool = True
    # ``steps`` is what the app sent before pages; a wall may still be
    # running that app when the server is updated.
    pages: list[TourPageIn] = Field(..., min_length=1,
                                    validation_alias=AliasChoices("pages", "steps"))
    #: None on an update keeps what the tour has; on create, the defaults.
    settings: TourSettings | None = None
    #: On update: the revision the editor loaded. A mismatch is a 409.
    expected_revision: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is empty")
        return v


class TourEnabledIn(BaseModel):
    enabled: bool
    expected_revision: int | None = None


def _custom_tiles(layout) -> int:
    """Tiles of a custom layout as Live View draws it (liveLayouts.ts
    resolveLayout): those inside its grid, or every cell when none is."""
    cols, rows = layout.grid_columns, layout.grid_rows
    n = 0
    for t in layout.tiles:
        r, c = t.get("row", 0), t.get("col", 0)
        rs, cs = t.get("rowSpan", 1), t.get("colSpan", 1)
        if r >= 0 and c >= 0 and rs >= 1 and cs >= 1 and r + rs <= rows and c + cs <= cols:
            n += 1
    return n or cols * rows


def _usable_layouts(db: Session) -> dict[str, int]:
    """Layouts Live View would offer right now and their tile counts:
    enabled predefined ones and enabled custom ones (a custom layout wins
    over a predefined one of the same id, as in Live View). A broken
    window-settings row must never break tours, so on any error only the
    predefined layouts count."""
    defaults = WindowDivisionSettings()
    try:
        from routers.window_settings import _load

        ws = _load(db)
    except Exception:
        logger.warning("window settings unreadable; tours use predefined layouts only",
                       exc_info=True)
        return {k: _PREDEFINED_TILES[k] for k in defaults.layouts_enabled if k in _PREDEFINED_TILES}
    layouts = {k: _PREDEFINED_TILES[k] for k in defaults.layouts_enabled
               if k in _PREDEFINED_TILES and ws.layouts_enabled.get(k, True)}
    layouts.update({c.id: _custom_tiles(c) for c in ws.custom_layouts if c.enabled})
    return layouts


def _tiles(layout_id: str | None, layouts: dict[str, int]) -> int | None:
    """How many tiles the layout has, or None when Live View no longer offers it."""
    if not layout_id:
        return None
    grid = _GRID_RE.fullmatch(layout_id)
    if grid:
        return int(grid.group(1)) * int(grid.group(2))
    return layouts.get(layout_id)


def _layout_ok(layout_id: str | None, layouts: dict[str, int]) -> bool:
    return _tiles(layout_id, layouts) is not None


def _split(page: dict, layouts: dict[str, int]) -> list[dict]:
    """*page* as the screenfuls the wall shows: itself, or, when it holds
    more cameras than its layout has tiles, consecutive pages of the same
    label, layout and time. A slice of empty tiles only is dropped; the
    wall skipped it anyway."""
    ids = list(page.get("camera_ids") or [])
    per = _tiles(page.get("layout_id"), layouts) or _AUTO_GRID_MAX
    if len(ids) <= per:
        return [page]
    out = []
    for i in range(0, len(ids), per):
        chunk = _trim(ids[i:i + per])
        if any(c is not None for c in chunk):
            out.append({**page, "camera_ids": chunk})
    return out


def _pages_of(tour: CameraTour, layouts: dict[str, int]) -> list[dict]:
    return [p for stored in (tour.pages or []) for p in _split(stored, layouts)]


class _Scope:
    """What every tour read and write needs, loaded once per request: the
    usable layouts and their tiles, every camera not in the bin (``{id: is_active}``, one
    query whatever the tours hold) and the caller's two camera scopes
    (``None`` = unrestricted)."""

    def __init__(self, db: Session, user: User):
        self.layouts = _usable_layouts(db)
        self.live = {cid: bool(active) for cid, active in
                     db.query(Camera.id, Camera.is_active)
                     .filter(Camera.deleted_at.is_(None)).all()}
        self.visible = visible_camera_ids(db, user)
        self.manageable = manageable_camera_ids(db, user)

    def editable(self, tour: CameraTour) -> bool:
        """May this caller change or delete the tour? Only when they may
        manage every camera still in it: a save carrying one they cannot
        manage is refused, and one they cannot see would be dropped."""
        if self.manageable is None:
            return True
        return all(int(c) in self.manageable
                   for p in (tour.pages or []) for c in (p.get("camera_ids") or [])
                   if c is not None and int(c) in self.live)


def _serialize(tour: CameraTour, scope: _Scope) -> dict:
    out_pages = []
    for p in _pages_of(tour, scope.layouts):
        ids = [None if c is None else int(c) for c in (p.get("camera_ids") or [])]
        # A camera in the bin, or one this viewer may not see, leaves its
        # tile empty rather than pulling the later cameras forward.
        slots = [c if c is not None and c in scope.live
                 and (scope.visible is None or c in scope.visible) else None for c in ids]
        shown = [c for c in slots if c is not None]
        out_pages.append({
            "label": p.get("label") or "",
            "layout_id": p.get("layout_id"),
            "duration_s": int(p.get("duration_s") or 60),
            "camera_ids": _trim(slots),
            "paused_camera_ids": [c for c in shown if not scope.live[c]],
            "hidden_count": sum(c is not None for c in ids) - len(shown),
            "layout_missing": not _layout_ok(p.get("layout_id"), scope.layouts),
        })
    return {
        "id": tour.id,
        "name": tour.name,
        "description": tour.description,
        "enabled": bool(tour.enabled),
        "revision": tour.revision or 1,
        "settings": _settings_of(tour),
        "editable": scope.editable(tour),
        "pages": out_pages,
        # The same list under its old name, for a wall still running the app
        # from before pages: each entry is one screenful, so it plays them
        # unchanged. Drop it a release after pages ship.
        "steps": out_pages,
        "created_at": tour.created_at.isoformat() if tour.created_at else None,
        "updated_at": tour.updated_at.isoformat() if tour.updated_at else None,
    }


def _validate(payload: TourIn, scope: _Scope) -> list[dict]:
    """The pages as stored, or a 422 naming exactly what is wrong."""
    bad_layouts = sorted({p.layout_id for p in payload.pages
                          if not _layout_ok(p.layout_id, scope.layouts)})
    if bad_layouts:
        raise HTTPException(status_code=422, detail={
            "message": "Unknown or disabled layout", "layout_ids": bad_layouts})
    wanted = {c for p in payload.pages for c in p.camera_ids if c is not None}
    bad = sorted(c for c in wanted if c not in scope.live
                 or (scope.manageable is not None and c not in scope.manageable))
    if bad:
        raise HTTPException(status_code=422, detail={
            "message": "Unknown cameras, or cameras you cannot manage", "camera_ids": bad})
    return [q for p in payload.pages for q in _split(p.model_dump(), scope.layouts)]


def _tour_or_404(db: Session, tour_id: int) -> CameraTour:
    tour = db.query(CameraTour).filter(CameraTour.id == tour_id).first()
    if tour is None:
        raise HTTPException(status_code=404, detail="Tour not found")
    return tour


def _editable_or_403(tour: CameraTour, scope: _Scope) -> None:
    if not scope.editable(tour):
        raise HTTPException(status_code=403,
                            detail="This tour includes cameras you cannot manage")


def _save(db: Session, tour_id: int, expected: int | None, values: dict) -> None:
    """Write *values* and bump the revision in ONE conditional UPDATE, so of
    two saves made from the same revision exactly one lands and the other is
    a 409 (a read-then-write check would let both through)."""
    q = db.query(CameraTour).filter(CameraTour.id == tour_id)
    if expected is not None:
        q = q.filter(CameraTour.revision == expected)
    n = q.update({**values, CameraTour.revision: CameraTour.revision + 1,
                  CameraTour.updated_at: func.now()}, synchronize_session=False)
    if n == 0:
        db.rollback()
        raise HTTPException(status_code=409, detail="The tour was changed by someone else")
    db.commit()


@router.get("")
async def list_tours(
    db: Session = Depends(get_db),
    current_user: User = Depends(_view),
):
    scope = _Scope(db, current_user)
    rows = db.query(CameraTour).order_by(CameraTour.name, CameraTour.id).all()
    return {"tours": [_serialize(t, scope) for t in rows]}


@router.get("/thumbnails/{camera_id}")
async def camera_thumbnail(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(_view),
):
    """A small, cached JPEG still of a camera for the tour editor's picker
    (services/camera_thumbnail.py). 404 for a camera the caller may not see
    or that is in the bin; 503 when no frame can be had (offline, paused)."""
    cam = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if cam is None or not can_view_camera(db, current_user, camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    if not cam.is_active or not (cam.rtsp_url or is_publisher(cam)):
        raise HTTPException(status_code=503, detail="Camera is not streaming")
    # A webcam has no URL: "" makes the grab read its MediaMTX path.
    cam_id, rtsp_url = cam.id, cam.rtsp_url or ""
    # The grab can take seconds; never hold a pooled connection across it.
    release(db)
    jpeg = await camera_thumbnail_service.get_thumbnail(cam_id, rtsp_url)
    if not jpeg:
        raise HTTPException(status_code=503, detail="No picture from this camera right now")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/{tour_id}")
async def get_tour(
    tour_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(_view),
):
    return _serialize(_tour_or_404(db, tour_id), _Scope(db, current_user))


@router.post("", status_code=201)
async def create_tour(
    payload: TourIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    scope = _Scope(db, current_user)
    pages = _validate(payload, scope)
    tour = CameraTour(name=payload.name, description=payload.description,
                      enabled=payload.enabled, pages=pages, revision=1,
                      settings=(payload.settings or TourSettings()).model_dump(),
                      created_by=getattr(current_user, "id", None))
    db.add(tour)
    db.commit()
    db.refresh(tour)
    audit_request(db, request, action="tour.create", user_id=current_user.id,
                  entity_type="camera_tour", entity_id=tour.id,
                  details={"name": tour.name, "pages": len(pages)})
    return _serialize(tour, scope)


@router.put("/{tour_id}")
async def update_tour(
    tour_id: int,
    payload: TourIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    tour = _tour_or_404(db, tour_id)
    scope = _Scope(db, current_user)
    _editable_or_403(tour, scope)
    pages = _validate(payload, scope)
    values = {CameraTour.name: payload.name, CameraTour.description: payload.description,
              CameraTour.enabled: payload.enabled, CameraTour.pages: pages}
    if payload.settings is not None:
        values[CameraTour.settings] = payload.settings.model_dump()
    _save(db, tour_id, payload.expected_revision, values)
    db.refresh(tour)
    audit_request(db, request, action="tour.update", user_id=current_user.id,
                  entity_type="camera_tour", entity_id=tour.id,
                  details={"name": tour.name, "pages": len(pages)})
    return _serialize(tour, scope)


@router.patch("/{tour_id}")
async def set_tour_enabled(
    tour_id: int,
    payload: TourEnabledIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    """Turn a tour on or off and nothing else. Its pages are not re-sent, so
    a tour whose layout was since deleted, or whose cameras are all in the
    bin, can still be switched, and nothing in it is rewritten."""
    tour = _tour_or_404(db, tour_id)
    scope = _Scope(db, current_user)
    _editable_or_403(tour, scope)
    _save(db, tour_id, payload.expected_revision, {CameraTour.enabled: payload.enabled})
    db.refresh(tour)
    audit_request(db, request, action="tour.update", user_id=current_user.id,
                  entity_type="camera_tour", entity_id=tour.id,
                  details={"name": tour.name, "enabled": payload.enabled})
    return _serialize(tour, scope)


@router.delete("/{tour_id}")
async def delete_tour(
    tour_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    tour = _tour_or_404(db, tour_id)
    _editable_or_403(tour, _Scope(db, current_user))
    name = tour.name
    db.delete(tour)
    db.commit()
    audit_request(db, request, action="tour.delete", user_id=current_user.id,
                  entity_type="camera_tour", entity_id=tour_id,
                  details={"name": name})
    return {"deleted": tour_id}
