# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Site mode (HA-118), v1 = arming only.

``disarmed``, ``armed_home`` or ``armed_away``. Home Assistant's alarm panel
drives it. What it changes in core, and nothing more:

* **disarmed**: alarm ACTIONS (phone call, SMS, external hooter/webhook) are
  not dispatched. Alerts are still stored, shown in the inbox and pushed to
  open browsers and to Home Assistant, so nothing is hidden: only the
  "wake someone up" path is paused.
* **armed_home / armed_away**: alarm actions dispatch exactly as configured.
  The two differ for Home Assistant automations, not inside core (v1).

A site that never set a mode is ``armed_away``: exactly today's behaviour,
where every alarm action dispatches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from services import site_settings

KEY = "site_mode"
MODES = ("disarmed", "armed_home", "armed_away")
DEFAULT_MODE = "armed_away"


def get(db: Session) -> dict[str, Any]:
    value = site_settings.get_json(db, KEY)
    if isinstance(value, dict) and value.get("mode") in MODES:
        return value
    return {"mode": DEFAULT_MODE, "changed_at": None, "changed_by": None}


def current_mode(db: Session) -> str:
    return get(db)["mode"]


def set_mode(db: Session, mode: str, actor: str) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    value = {"mode": mode, "changed_at": datetime.now(UTC).isoformat(), "changed_by": actor}
    site_settings.set_json(db, KEY, value)
    return value


def alarm_actions_allowed(db: Session) -> bool:
    return current_mode(db) != "disarmed"
