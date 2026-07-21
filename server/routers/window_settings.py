# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Window-layout preferences for the Live View grid.

This is the one live piece of the old ``general`` router: it is consumed by
``LiveView.tsx`` (default layout + which layouts are offered) and edited from
Settings > More Settings > Window Settings. The rest of the ``general`` router
stored config that nothing read and was removed; this endpoint is kept because
it is genuinely used.

Persisted as a single row in ``security_settings`` (a generic key/JSON store),
matching how the original endpoint stored it — so no migration is needed.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import SecuritySetting
from schemas import WindowDivisionSettings

router = APIRouter(prefix="/general", tags=["window-settings"])

_KEY = "window_division_settings"


def _load(db: Session) -> WindowDivisionSettings:
    """Load stored settings merged over defaults (so new default keys appear)."""
    row = db.query(SecuritySetting).filter(SecuritySetting.key == _KEY).first()
    stored = {}
    if row and row.json_value:
        try:
            stored = json.loads(row.json_value)
        except Exception:
            stored = {}
    return WindowDivisionSettings(**{**WindowDivisionSettings().model_dump(), **stored})


@router.get("/window-settings")
async def get_window_settings(
    db: Session = Depends(get_db),
    _user=Depends(get_current_active_user),
):
    """Read the Live View window-layout preferences (any authenticated user)."""
    return _load(db).model_dump()


@router.put("/window-settings")
async def update_window_settings(
    payload: WindowDivisionSettings,
    db: Session = Depends(get_db),
    _user=Depends(get_current_active_user),
):
    """Update the Live View window-layout preferences."""
    row = db.query(SecuritySetting).filter(SecuritySetting.key == _KEY).first()
    if not row:
        row = SecuritySetting(key=_KEY, json_value="{}")
        db.add(row)
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    return payload.model_dump()
