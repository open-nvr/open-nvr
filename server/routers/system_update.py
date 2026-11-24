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
* ``POST /system/update/apps/{id}/request`` — install an app's newest
  release from the registry now; ``DELETE /system/update/apps/request``
  withdraws it. ``system.update``. ``GET`` carries the apps under ``apps``.
* ``POST /system/update/check`` — look for new releases (OpenNVR's and the
  apps') now rather than at the next six-hourly check. Installs nothing.
  ``system.update``.

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
_APP_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class UpdatePolicy(BaseModel):
    #: off: never look; notify: look and say so; manual: also allow Update
    #: now; auto (the default): also install releases by itself, inside the
    #: window. Only signed stable releases, with a backup and rollback.
    mode: Literal["off", "notify", "manual", "auto"] = "auto"
    channel: Literal["stable"] = "stable"
    #: How big an update "auto" may install by itself (never a major one).
    auto_scope: Literal["patch", "minor"] = "minor"
    #: Site-local days and times the host may install in (auto only).
    window_days: str = "mon,tue,wed,thu,fri,sat,sun"
    window_start: str = "02:00"
    window_end: str = "04:00"
    #: Apps released from the registry (docs/design/app-updates-from-registry.md).
    #: "" = the same as ``mode`` / ``auto_scope`` above; ``app_modes`` sets
    #: one app's mode over both. Same window; a shorter per-site delay.
    apps_mode: Literal["", "off", "notify", "manual", "auto"] = ""
    apps_auto_scope: Literal["", "patch", "minor"] = ""
    app_modes: dict[str, Literal["off", "notify", "manual", "auto"]] = {}

    @field_validator("app_modes")
    @classmethod
    def _app_ids(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 100 or any(not _APP_ID.match(k) for k in v):
            raise ValueError("app_modes is {app id: mode}, app ids in kebab-case")
        return dict(sorted(v.items()))

    def app_mode(self, app_id: str) -> str:
        """This app's mode: its own, else the apps mode, else the release mode
        (the same order as updater/policy.py)."""
        return self.app_modes.get(app_id) or self.apps_mode or self.mode

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
    except Exception:  # an unreadable row falls back to the default (auto)
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


@router.post("/check", status_code=202)
async def request_check(request: Request, db: Session = Depends(get_db),
                        current_user: User = Depends(_update)):
    """Check now: the host runner looks for new releases on its next tick
    (within a minute) and this page shows what it found. Nothing is
    installed; an automatic install still waits for the window."""
    version, commit = _installed()
    now = update_status.status(version, commit)
    if not now["connected"]:
        raise HTTPException(status_code=503, detail="This installation has no update folders mounted.")
    if not now["runner"]["running"]:
        raise HTTPException(status_code=409, detail="The update runner is not running on the host.")
    if now["pending_check"]:
        return {"request": None, "already_requested": True}
    rid = update_status.request_check(current_user.username)
    audit_request(db, request, action="system.update_check", user_id=current_user.id,
                  entity_type="system", entity_id=None, details={"request": rid})
    return {"request": rid, "already_requested": False}


@router.delete("/request")
async def cancel_update_request(request: Request, db: Session = Depends(get_db),
                                current_user: User = Depends(_update)):
    if not update_status.cancel_request():
        raise HTTPException(status_code=404, detail="No update is waiting to start.")
    audit_request(db, request, action="system.update_cancel", user_id=current_user.id,
                  entity_type="system", entity_id=None, details={})
    return {"cancelled": True}


@router.post("/apps/{app_id}/request", status_code=202)
async def request_app_update(app_id: str, request: Request, db: Session = Depends(get_db),
                             current_user: User = Depends(_update)):
    """Install this app's newest release now (Update now). The host checks
    again and verifies the signature itself; this only asks."""
    if not _APP_ID.match(app_id):
        raise HTTPException(status_code=404, detail="No such app.")
    mode = _policy(db).app_mode(app_id)
    if mode not in ("manual", "auto"):
        raise HTTPException(status_code=409, detail=f"Updates for {app_id} are '{mode}' in the update policy.")
    version, commit = _installed()
    now = update_status.status(version, commit)
    if not now["connected"]:
        raise HTTPException(status_code=503, detail="This installation has no update folders mounted.")
    if (now["run"] and now["run"]["in_progress"]) or (now["apps"]["run"] and now["apps"]["run"]["in_progress"]):
        raise HTTPException(status_code=409, detail="An update is already running.")
    if now["apps"]["pending_request"]:
        raise HTTPException(status_code=409, detail="An app update is already requested.")
    app = next((a for a in now["apps"]["apps"] if a["id"] == app_id), None)
    if app is None or not app["available"]:
        raise HTTPException(status_code=409, detail=f"No newer release of {app_id} was found on this site.")
    if app["available"]["signature"] == "failed":
        raise HTTPException(status_code=409, detail=f"{app_id} {app['available']['version']}'s signature did not "
                            f"check out: {app['available']['signature_message']}")
    rid = update_status.request_app_update(app_id, current_user.username)
    audit_request(db, request, action="system.app_update_request", user_id=current_user.id,
                  entity_type="app", entity_id=None,
                  details={"app": app_id, "from": app["installed_version"], "to": app["available"]["version"],
                           "request": rid})
    return {"request": rid, "app": app_id, "to": app["available"]["version"]}


@router.delete("/apps/request")
async def cancel_app_update_request(request: Request, db: Session = Depends(get_db),
                                    current_user: User = Depends(_update)):
    if not update_status.cancel_app_request():
        raise HTTPException(status_code=404, detail="No app update is waiting to start.")
    audit_request(db, request, action="system.app_update_cancel", user_id=current_user.id,
                  entity_type="app", entity_id=None, details={})
    return {"cancelled": True}
