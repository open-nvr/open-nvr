# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""OpenNVR updates in the app (docs/design/auto-update-implementation-plan.md, M3).

* ``GET /system/update`` — installed version, the update the host found, the
  run in progress or the last one, history, whether the host runner is
  alive, and the policy. Needs ``settings.view``.
* ``PUT /system/update/policy`` — off / notify / manual / auto, the window
  and how big an automatic update may be. Needs ``system.update``.
* ``POST /system/update/request`` — install the update the host found.
  ``DELETE`` withdraws it until the runner picks it up. ``system.update``.

The host does the work (services/update_status.py explains the hand-off).
These are not the OS updates of routers/firmware.py.
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from core.database import get_db
from core.permissions import RequirePermission
from models import SecuritySetting, User
from services import update_status
from services.audit_service import audit_request

router = APIRouter(prefix="/system/update", tags=["system"])

_view = RequirePermission("settings.view")
_update = RequirePermission("system.update")

POLICY_KEY = "opennvr_update_policy"
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class UpdatePolicy(BaseModel):
    #: off: never look; notify: look and say so; manual: also allow Update
    #: now; auto: also install, inside the window.
    mode: Literal["off", "notify", "manual", "auto"] = "off"
    channel: Literal["stable"] = "stable"
    #: How big an update "auto" may install by itself.
    auto_scope: Literal["patch", "minor"] = "patch"
    #: Site-local days and times the host may install in (auto only).
    window_days: str = "sun"
    window_start: str = "02:00"
    window_end: str = "04:00"

    @field_validator("window_days")
    @classmethod
    def _days(cls, v: str) -> str:
        days = [d.strip().lower() for d in v.split(",") if d.strip()]
        if not days or any(d not in _DAYS for d in days):
            raise ValueError(f"window_days is a comma list of {', '.join(_DAYS)}")
        return ",".join(dict.fromkeys(days))

    @field_validator("window_start", "window_end")
    @classmethod
    def _time(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("times are HH:MM, 24-hour")
        return v


class UpdateRequestIn(BaseModel):
    version: str


def _policy(db: Session) -> UpdatePolicy:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == POLICY_KEY).first()
    if row is None:
        return UpdatePolicy()
    try:
        return UpdatePolicy(**json.loads(row.json_value))
    except Exception:  # an unreadable row falls back to the safe default (off)
        return UpdatePolicy()


def _installed() -> tuple[str, str | None]:
    from main import __version__

    return __version__, os.environ.get("OPENNVR_BUILD_COMMIT") or None


@router.get("")
async def get_update_status(db: Session = Depends(get_db), current_user: User = Depends(_view)):
    version, commit = _installed()
    out = update_status.status(version, commit)
    out["policy"] = _policy(db).model_dump()
    return out


@router.put("/policy")
async def set_update_policy(policy: UpdatePolicy, request: Request, db: Session = Depends(get_db),
                            current_user: User = Depends(_update)):
    row = db.query(SecuritySetting).filter(SecuritySetting.key == POLICY_KEY).first()
    value = json.dumps(policy.model_dump())
    if row is None:
        db.add(SecuritySetting(key=POLICY_KEY, json_value=value))
    else:
        row.json_value = value
    db.commit()
    try:
        update_status.write_policy(policy.model_dump())
    except OSError as exc:
        # Saved, but the host will not see it until the folder is mounted.
        raise HTTPException(status_code=503, detail=f"Saved, but the host could not be told: {exc}") from exc
    audit_request(db, request, action="system.update_policy", user_id=current_user.id,
                  entity_type="system", entity_id=None, details=policy.model_dump())
    return policy.model_dump()


@router.post("/request", status_code=202)
async def request_update(body: UpdateRequestIn, request: Request, db: Session = Depends(get_db),
                         current_user: User = Depends(_update)):
    if _policy(db).mode == "off":
        raise HTTPException(status_code=409, detail="Updates are turned off in the update policy.")
    version, commit = _installed()
    now = update_status.status(version, commit)
    if not now["connected"]:
        raise HTTPException(status_code=503, detail="This installation has no update folders mounted.")
    if now["run"] and now["run"]["in_progress"]:
        raise HTTPException(status_code=409, detail="An update is already running.")
    if now["pending_request"]:
        raise HTTPException(status_code=409, detail="An update is already requested.")
    if not now["available"] or now["available"]["version"] != body.version:
        raise HTTPException(status_code=409, detail=f"{body.version} is not the update this site found.")
    if now["available"]["manual_steps"]:
        raise HTTPException(status_code=409, detail="This update needs steps on the host first: "
                            + " ".join(now["available"]["manual_steps"]))
    rid = update_status.request_update(body.version, current_user.username)
    audit_request(db, request, action="system.update_request", user_id=current_user.id,
                  entity_type="system", entity_id=None,
                  details={"from": version, "to": body.version, "request": rid})
    return {"request": rid, "to": body.version}


@router.delete("/request")
async def cancel_update_request(request: Request, db: Session = Depends(get_db),
                                current_user: User = Depends(_update)):
    if not update_status.cancel_request():
        raise HTTPException(status_code=404, detail="No update is waiting to start.")
    audit_request(db, request, action="system.update_cancel", user_id=current_user.id,
                  entity_type="system", entity_id=None, details={})
    return {"cancelled": True}
