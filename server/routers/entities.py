# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Server-described entities (HA-114): descriptors, states and typed commands.

* ``GET /entities`` — the descriptors the caller may see, with an ``ETag``
  (``If-None-Match`` → 304). ``descriptors_changed`` on the v2 socket says
  when to re-fetch.
* ``GET /entities/states`` — the last resolved state of each of them (the
  same values the v2 socket pushes as ``entity_state``).
* ``POST /entities/{key}/command`` — run a descriptor's command. Only a
  descriptor the caller can SEE can be commanded (its required_scope and
  camera are checked by that); the command is typed, never a URL, and each
  control re-checks what it touches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import Camera, User
from services import entity_descriptors as ed
from services.audit_service import audit_request

router = APIRouter(prefix="/entities", tags=["entities"])

#: Direction → (pan, tilt, zoom) velocity for a short PTZ nudge.
_PTZ_VECTORS = {"up": (0, 0.5, 0), "down": (0, -0.5, 0), "left": (-0.5, 0, 0),
                "right": (0.5, 0, 0), "zoom_in": (0, 0, 0.5), "zoom_out": (0, 0, -0.5)}
PTZ_NUDGE_S = 0.4


class CommandIn(BaseModel):
    #: switch: bool; select: an option; number: a number; button: omitted.
    value: Any = None
    args: dict[str, Any] = Field(default_factory=dict)


@router.get("")
async def list_entities(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    descs = ed.descriptors_for(db, current_user)
    etag = f'"{ed.etag_of(descs)}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return {"etag": etag.strip('"'), "descriptor_version": ed.DESCRIPTOR_VERSION,
            "entities": [d.to_dict() for d in descs]}


@router.get("/states")
async def entity_states(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    from services import entity_state_publisher as pub

    pub.note_rest_use()
    descs = ed.descriptors_for(db, current_user)
    cached = pub.current_states()
    keys = {d.key for d in descs if ed.has_state(d)}
    if pub.current_etag() is None:
        # The publisher has not run yet (just after start, or idle with no
        # client): resolve now, off the event loop, for this caller only.
        import asyncio

        cached = await asyncio.to_thread(_resolve_for, descs)
    return {"states": {k: cached[k] for k in keys if k in cached}}


def _resolve_for(descs) -> dict:
    from core.database import SessionLocal

    with SessionLocal() as db:
        return ed.resolve_states(db, descs)


def _camera(db: Session, desc: ed.Descriptor, principal=None) -> Camera:
    """The descriptor's camera. With ``principal``, also the right to
    CHANGE it: the same owner rule ``PUT /cameras/{id}`` applies
    (get_camera_or_403), not just seeing it."""
    cam = db.query(Camera).filter(Camera.id == desc.camera_id,
                                  Camera.deleted_at.is_(None)).first()
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if principal is not None:
        from core.permissions import PermissionChecker

        PermissionChecker(Camera).check(cam.id, principal, db)
    return cam


def _actor(principal) -> str:
    from core.request_context import current as current_ctx

    ctx = current_ctx()
    return (ctx.actor if ctx is not None and ctx.actor else None) or f"user:{principal.username}"


async def _core_control(db: Session, principal, desc: ed.Descriptor, body: CommandIn,
                        request: Request) -> dict:
    control = desc.command.get("control")
    if control == "detection":
        if not isinstance(body.value, bool):
            raise HTTPException(status_code=422, detail="value must be true or false")
        cam = _camera(db, desc, principal)
        cam.detection_enabled = body.value
        db.commit()
        return {"detection_enabled": cam.detection_enabled}
    if control == "recording_pause":
        from services import recording_pause
        from services.site_settings import recording_pause_enabled

        if not isinstance(body.value, bool):
            raise HTTPException(status_code=422, detail="value must be true or false")
        cam = _camera(db, desc, principal)
        reason = body.args.get("reason")
        reason = str(reason)[:200] if reason is not None else None
        if body.value:
            await recording_pause.resume(db, cam.id, actor=_actor(principal), reason=reason)
            return {"recording": True}
        if not recording_pause_enabled(db):
            raise HTTPException(status_code=403,
                                detail="Pausing recording is not allowed on this site")
        after = body.args.get("resume_after_s")
        if after is not None and (isinstance(after, bool) or not isinstance(after, int)):
            raise HTTPException(status_code=422, detail="resume_after_s must be an integer")
        try:
            state = await recording_pause.pause(db, cam.id, actor=_actor(principal),
                                                reason=reason, resume_after_s=after)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"recording": False, "paused": state}
    if control == "manual_event":
        from services.timeline_service import record_manual_event

        from routers.timeline_events import _LABEL_RE

        cam = _camera(db, desc)
        label = str(body.args.get("label") or "manual").strip().lower()
        if not _LABEL_RE.fullmatch(label):
            raise HTTPException(status_code=422,
                                detail="label: lowercase letters, digits, space, _ . - only")
        note = body.args.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise HTTPException(status_code=422, detail="note: text, at most 500 characters")
        row = record_manual_event(db, camera_id=cam.id, label=label,
                                  started_at=datetime.now(UTC), note=note,
                                  actor=_actor(principal))
        return {"event_id": row.id}
    if control == "ack_alerts":
        from models import AppAlert
        from routers.alerts_inbox import _scope_alerts
        from services.camera_scope import visible_camera_ids

        rows = (_scope_alerts(db.query(AppAlert), visible_camera_ids(db, principal))
                .filter(AppAlert.acknowledged_at.is_(None)).all())
        now = datetime.now(UTC)
        for row in rows:
            row.acknowledged_at = now
            row.acknowledged_by = principal.id
        db.commit()
        return {"acknowledged": len(rows)}
    if control in ("ptz_move", "ptz_preset"):
        import asyncio

        from routers.cameras import _ptz_camera
        from services import ptz_presets_cache
        from services.ptz_service import PTZService

        cam = _ptz_camera(db, desc.camera_id, principal)
        kw = dict(camera_id=cam.id, ip=cam.ip_address, username=cam.username,
                  password=cam.password, camera_port=cam.port)
        if control == "ptz_move":
            x, y, z = _PTZ_VECTORS[desc.command["args"]["direction"]]
            await PTZService.move(**kw, x=x, y=y, z=z)
            await asyncio.sleep(PTZ_NUDGE_S)
            await PTZService.stop(**kw)
            return {"moved": desc.command["args"]["direction"]}
        token = ptz_presets_cache.token_for(cam.id, str(body.value))
        if token is None:
            raise HTTPException(status_code=422, detail="Unknown preset")
        await PTZService.goto_preset(**kw, preset_token=token)
        return {"preset": body.value}
    raise HTTPException(status_code=400, detail=f"Unsupported control {control!r}")


async def _app_action(db: Session, principal, desc: ed.Descriptor, body: CommandIn) -> Any:
    from models import InstalledApp
    from routers.apps import invoke_app_action

    app_id = desc.origin[4:]
    row = db.query(InstalledApp).filter(InstalledApp.id == app_id).first()
    declared = {a.get("name"): a for a in (row.manifest_json or {}).get("actions") or []} \
        if row else {}
    action = declared.get(desc.command["action"])
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    names = {p.get("name") for p in action.get("params") or []}
    params = {k: v for k, v in body.args.items() if k in names}
    if desc.camera_id is not None and "camera" in names:
        params["camera"] = f"cam{desc.camera_id}"
    if body.value is not None and "value" in names:
        params["value"] = body.value
    return await invoke_app_action(app_id, desc.command["action"], params,
                                   current_user=principal, db=db)


@router.post("/{key}/command")
async def command_entity(
    key: str,
    body: CommandIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    desc = next((d for d in ed.descriptors_for(db, current_user) if d.key == key), None)
    # 404 whether it doesn't exist or the caller may not see it.
    if desc is None or not desc.command:
        raise HTTPException(status_code=404, detail="No such entity command")
    kind = desc.command.get("type")
    if kind == "core_control":
        result = await _core_control(db, current_user, desc, body, request)
    elif kind == "app_action":
        result = await _app_action(db, current_user, desc, body)
    else:
        raise HTTPException(status_code=400, detail="Unsupported command")
    audit_request(db, request, action="entity.command", user_id=current_user.id,
                  entity_type="entity", entity_id=None,
                  details={"key": key, "command": desc.command, "value": body.value,
                           "camera_id": desc.camera_id})
    return {"key": key, "result": result}
