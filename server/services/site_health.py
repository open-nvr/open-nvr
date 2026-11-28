# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A remote site's health, in plain words, and its alerts (plan S7b).

- Problems: what each relayed camera's last error means (a wrong password,
  a camera the agent cannot reach, a stream that stopped), and what is wrong
  with the site machine itself (its clock, its temperature, its disk), for
  the Sites page.
- Alerts: a site offline for OFFLINE_AFTER_S, or one of its cameras down for
  CAMERA_DOWN_AFTER_S, becomes a host-level alert like a full disk
  (services/system_events.py: kept in the system events and pushed live as
  ``system_alert``), once when it starts and once when it is over. Each kind
  can be switched off per site.
- Checks: an admin's "Run checks" asks the agent to test, from the site,
  the server, the video route and each camera; the answer comes with its
  next report.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

OFFLINE_AFTER_S = 5 * 60
CAMERA_DOWN_AFTER_S = 10 * 60
CLOCK_SKEW_MAX_S = 120
HOT_C = 80.0
DISK_LOW_MB = 200

# ── plain-language problems ─────────────────────────────────────────────

_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("wrong_password", re.compile(r"\b401\b|unauthori[sz]ed|wrong password", re.I),
     "The camera refused its user and password."),
    ("not_relayed", re.compile(r"^not relayed", re.I),
     "The site's agent will not relay this address (see the detail)."),
    ("unreachable", re.compile(r"connection refused|no route|network is unreachable|i/o timeout|"
                               r"cannot resolve|no such host|host is down|deadline exceeded", re.I),
     "The site's agent cannot reach the camera."),
    ("stream_ended", re.compile(r"sent nothing|\bEOF\b|stream ended|terminated", re.I),
     "The camera stopped sending."),
    ("server", re.compile(r"publish token|^server:|tls|certificate", re.I),
     "The stream does not get through to this server."),
)


def camera_problem(state: str | None, detail: str | None) -> dict[str, str] | None:
    """What a relayed stream's state means, or None when it is fine."""
    if state == "live" or state in (None, "starting"):
        return None
    if state == "uplink_cap":
        return {"code": "paused_cap", "text": "Paused: the site is over its uplink cap."}
    if state == "waiting":
        return {"code": "waiting", "text": "Waiting for the connection to this server."}
    d = detail or ""
    for code, pattern, text in _RULES:
        if pattern.search(d):
            return {"code": code, "text": text}
    return {"code": "error", "text": "The stream is not working."}


def box_problems(health: dict[str, Any] | None, skew_s: float | None) -> list[dict[str, str]]:
    """What is wrong with the site machine itself."""
    out = []
    if skew_s is not None and abs(skew_s) > CLOCK_SKEW_MAX_S:
        out.append({"code": "clock", "text": f"The site machine's clock is {abs(skew_s) / 60:.0f} minutes "
                                             f"{'ahead' if skew_s > 0 else 'behind'}: recordings would carry "
                                             "the wrong time and secure connections can fail."})
    h = health or {}
    temp = h.get("temp_c")
    if isinstance(temp, (int, float)) and temp >= HOT_C:
        out.append({"code": "hot", "text": f"The site machine is hot ({temp:.0f} °C): it may slow down or "
                                           "switch off. Give it air or a fan."})
    free = h.get("disk_free_mb")
    if isinstance(free, (int, float)) and free < DISK_LOW_MB:
        out.append({"code": "disk", "text": f"The site machine's disk is nearly full ({free:.0f} MB free)."})
    return out


def clock_skew(report: dict[str, Any], received_at: float | None) -> float | None:
    """How far the agent's clock is from this server's (seconds, + ahead),
    from the time it wrote in its last report and when that arrived."""
    now = report.get("now")
    if not isinstance(now, (int, float)) or not received_at:
        return None
    return float(now) - float(received_at)


# ── checks ───────────────────────────────────────────────────────────────

#: The latest "Run checks" per site (milliseconds; in memory).
_check_requests: dict[int, int] = {}


def request_checks(site_id: int) -> int:
    from services.sites import notify

    rid = int(time.time() * 1000)
    _check_requests[site_id] = rid
    notify(site_id)
    return rid


def checks_request(site_id: int) -> int | None:
    return _check_requests.get(site_id)


def checks_view(site_id: int, report: dict[str, Any]) -> dict[str, Any]:
    got = report.get("checks") or {}
    asked = checks_request(site_id)
    return {
        "requested": asked,
        "pending": bool(asked) and got.get("id") != asked,
        "at": got.get("at"),
        "items": got.get("items") or [],
    }


# ── alerts ───────────────────────────────────────────────────────────────


def _since(ts: Any) -> float | None:
    return float(ts) if isinstance(ts, (int, float)) else None


def tick(db: Session, now: float | None = None) -> list[dict[str, Any]]:
    """One pass over the enrolled sites; returns the alert rows written
    (edge-triggered: only when a site or camera changes state)."""
    import json

    from models import Site
    from services.sites import _aware, site_cameras
    from services.system_events import record_system_event_edge

    now = now if now is not None else time.time()
    rows: list[dict[str, Any]] = []
    for site in db.query(Site).filter(Site.credential_hash.isnot(None)).all():
        last = _aware(site.last_seen_at)
        offline_s = now - last.timestamp() if last else None
        offline = site.alert_offline and offline_s is not None and offline_s > OFFLINE_AFTER_S
        row = record_system_event_edge(
            event_type=f"site_offline:{site.id}", active=offline, severity="warning",
            description=(f"Site \"{site.name}\" is offline: its agent has not checked in for "
                         f"{int((offline_s or 0) // 60)} minutes" if offline else f"Site \"{site.name}\" is back online"),
            data={"site_id": site.id, "site": site.name},
        )
        if row:
            rows.append(row)
        try:
            report = json.loads(site.report) if site.report else {}
        except ValueError:
            report = {}
        states = {p.get("camera_id"): p for p in (report.get("publishers") or [])
                  if isinstance(p, dict) and p.get("stream") == "main"}
        for cam in site_cameras(db, site.id):
            st = states.get(cam.id) or {}
            since = _since(st.get("since"))
            bad = st.get("state") in ("error", "waiting")
            # While the site is offline the site's alert says it all.
            down = (site.alert_camera_down and not offline and cam.is_active and bad
                    and since is not None and now - since > CAMERA_DOWN_AFTER_S)
            problem = camera_problem(st.get("state"), st.get("detail")) if down else None
            row = record_system_event_edge(
                event_type=f"site_camera_down:{cam.id}", active=down, severity="warning",
                description=(f"Camera \"{cam.name}\" at site \"{site.name}\" is down: {problem['text']}"
                             if down and problem else f"Camera \"{cam.name}\" at site \"{site.name}\" works again"),
                data={"site_id": site.id, "camera_id": cam.id, "problem": problem},
            )
            if row:
                rows.append(row)
    return rows


async def run_forever(interval: float = 60.0) -> None:
    """The background loop main.py starts."""
    import asyncio

    from core.database import SessionLocal
    from core.logging_config import main_logger
    from services.event_bus_service import publish_system_alert

    while True:
        await asyncio.sleep(interval)
        try:
            db = SessionLocal()
            try:
                rows = await asyncio.to_thread(tick, db)
            finally:
                db.close()
            for row in rows:
                await publish_system_alert(alert_type=row["event_type"].split(":", 1)[0],
                                           state=row["event_state"], severity=row["severity"], payload=row)
        except Exception:  # never take core down over a site alert
            main_logger.warning("site health pass failed", exc_info=True)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
