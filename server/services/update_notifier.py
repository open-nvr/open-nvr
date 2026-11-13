# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Telling people about OpenNVR updates (UPD-403).

The host updater writes each finished run to ``history.log`` and each
check's result to the state folder core reads (services/update_status.py).
Once a minute this turns what is new there into the same host-level alerts
disk pressure uses (services/system_events.py, published live as
``system_alert``): an update that went through, one rolled back, one that
failed, and an update found for the first time (what "tell me" promises).

What was already told is kept in the database, so the restart every update
causes does not repeat it; on its first run it marks the history as seen
without alerting about runs from before it existed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from core.logging_config import main_logger
from models import SecuritySetting

EVENT_TYPE = "opennvr_update"
_SEEN_KEY = "opennvr_update_notified"

SEVERITY = {"succeeded": "info", "rolled_back": "warning", "failed": "critical", "available": "info"}


@dataclass(frozen=True)
class Notice:
    state: str          # succeeded | rolled_back | failed | available
    severity: str
    description: str
    data: dict


def new_notices(history: list[str], available: str | None, seen: dict | None) -> tuple[list[Notice], dict]:
    """(what to tell, what has now been told). Pure.

    *history* is history.log's lines (``finished from to result``);
    *available* the version the last check found, if any; *seen* what was
    already told (None the first time: nothing is told, all is marked)."""
    runs = [p for p in (line.split() for line in history) if len(p) >= 4]
    last_run = runs[-1][0] if runs else ""
    if seen is None:
        return [], {"last_run": last_run, "available": available or ""}
    out: list[Notice] = []
    for finished, frm, to, result in (r[:4] for r in runs):
        if finished <= seen.get("last_run", ""):
            continue
        text = {
            "succeeded": f"OpenNVR was updated from {frm} to {to}.",
            "rolled_back": f"The update to OpenNVR {to} failed and was rolled back: {frm} runs again, with its data.",
            "failed": f"The update to OpenNVR {to} failed and could not be rolled back automatically. "
                      "A backup is in the backups folder on the host.",
        }.get(result)
        if text:
            out.append(Notice(result, SEVERITY[result], text, {"from": frm, "to": to, "finished": finished}))
    if available and available != seen.get("available"):
        out.append(Notice("available", "info", f"OpenNVR {available} is available.", {"version": available}))
    return out, {"last_run": max(last_run, seen.get("last_run", "")), "available": available or ""}


def _load_seen(db: Session) -> dict | None:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == _SEEN_KEY).first()
    try:
        return json.loads(row.json_value) if row else None
    except ValueError:
        return None


def _save_seen(db: Session, seen: dict) -> None:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == _SEEN_KEY).first()
    if row is None:
        db.add(SecuritySetting(key=_SEEN_KEY, json_value=json.dumps(seen)))
    else:
        row.json_value = json.dumps(seen)
    db.commit()


def tick(db: Session, installed_version: str, installed_commit: str | None) -> list[dict]:
    """One pass: record what is new, return the rows to publish live."""
    from services import update_status
    from services.system_events import record_system_event

    log = update_status.STATE_DIR / "history.log"
    history = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []
    status = update_status.status(installed_version, installed_commit)
    available = status["available"]["version"] if status["available"] else None
    notices, seen = new_notices(history, available, _load_seen(db))
    rows = []
    for n in notices:
        row = record_system_event(db, event_type=EVENT_TYPE, state=n.state, severity=n.severity,
                                  description=n.description, data=n.data)
        if row:
            rows.append(row)
    _save_seen(db, seen)
    return rows


async def run_forever(interval: float = 60.0) -> None:
    """The background loop main.py starts."""
    import asyncio
    import os

    from core.database import SessionLocal
    from services.event_bus_service import publish_system_alert

    while True:
        try:
            from main import __version__

            db = SessionLocal()
            try:
                rows = tick(db, __version__, os.environ.get("OPENNVR_BUILD_COMMIT") or None)
            finally:
                db.close()
            for row in rows:
                await publish_system_alert(alert_type=EVENT_TYPE, state=row["event_state"],
                                           severity=row["severity"], payload=row)
        except Exception:  # never take core down over a notice
            main_logger.warning("update notifier pass failed", exc_info=True)
        await asyncio.sleep(interval)
