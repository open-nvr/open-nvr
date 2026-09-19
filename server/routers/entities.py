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

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import User
from services import entity_descriptors as ed
from services.audit_service import audit_request

router = APIRouter(prefix="/entities", tags=["entities"])

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


@router.post("/{key}/command")
async def command_entity(
    key: str,
    body: CommandIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    from services.entity_commands import run_command

    desc, result = await run_command(db, current_user, key, body.value, body.args)
    audit_request(db, request, action="entity.command", user_id=current_user.id,
                  entity_type="entity", entity_id=None,
                  details={"key": key, "command": desc.command, "value": body.value,
                           "camera_id": desc.camera_id})
    return {"key": key, "result": result}
