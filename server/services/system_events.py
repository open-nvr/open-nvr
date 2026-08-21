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

"""Synchronous system-event recorder.

Small on purpose: retention and the monitor both run their DB work in worker
threads, so the write path must be plain sync SQLAlchemy with no event-bus /
asyncio imports. Publishing the live copy to the bus is the caller's job
(from async context), via services.event_bus_service.publish_system_alert.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.logging_config import main_logger
from models import SystemEvent


def latest_event_state(db: Session, event_type: str) -> str | None:
    """State of the most recent event of this type (None if no events)."""
    row = (
        db.query(SystemEvent.event_state)
        .filter(SystemEvent.event_type == event_type)
        .order_by(SystemEvent.id.desc())
        .first()
    )
    return row[0] if row else None


def record_system_event_edge(
    *,
    event_type: str,
    active: bool,
    severity: str = "warning",
    description: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Edge-triggered write: only persists when the state actually changes
    (same discipline as the recording watchdog). Returns the written row dict
    or None when nothing changed / the write failed."""
    db = SessionLocal()
    try:
        prev_active = latest_event_state(db, event_type) == "active"
        if active == prev_active:
            return None
        return record_system_event(
            db,
            event_type=event_type,
            state="active" if active else "inactive",
            severity=severity if active else "info",
            description=description,
            data=data,
        )
    except Exception as e:
        main_logger.warning(f"Edge event write failed for {event_type}: {e}")
        return None
    finally:
        db.close()


def record_system_event(
    db: Session | None = None,
    *,
    event_type: str,
    state: str | None,
    severity: str = "warning",
    description: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Persist one SystemEvent row. Best-effort: never raises.

    Returns the row as a dict (for bus publishing by an async caller), or
    None when the write failed.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        now = datetime.now(UTC)
        row = SystemEvent(
            event_type=event_type,
            event_state=state,
            severity=severity,
            description=(description or "")[:300] or None,
            data=json.dumps(data) if data else None,
            occurred_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "event_type": row.event_type,
            "event_state": row.event_state,
            "severity": row.severity,
            "description": row.description,
            "data": data,
            "occurred_at": now.isoformat(),
        }
    except Exception as e:
        db.rollback()
        main_logger.warning(f"Failed to record system event {event_type}: {e}")
        return None
    finally:
        if close_db:
            db.close()
