# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The escalation proposal: when a camera's evidence is weak on the sub
stream, propose detecting on the main stream — and only propose.

Tier-0 detects on a camera's sub stream and fetches the best crop of each
visit from the main-stream recording. Every posted visit says where its
crop came from and how big the subject was on the detect frame
(``events.payload["evidence"]``). Read over a day, that is the per-camera
signal this module turns into a proposal:

* **low-res evidence** — fewer than ``FULL_RES_MIN`` of the visits got a
  full-resolution crop (the recording refused, or was missing);
* **small subjects** — more than ``SMALL_SHARE_MAX`` of the visits had a
  subject under ``SMALL_PX`` on the detect frame, which the sub stream's
  resolution cannot fix and the main stream's can.

Either, over at least ``MIN_VISITS`` visits in ``WINDOW_HOURS``, on a
camera not already pinned to the main stream, is a proposal. It is sticky
and operator-visible (the camera list, the detection settings page) and
costs nothing until accepted: detecting on the main stream decodes the
full-resolution stream continuously, roughly five times the sub stream's
work on a typical 1080p/D1 pair (docs/DETECT_CPU.md), for as long as it
holds. So: propose by default, switch on consent.

Decisions are kept in one site setting keyed by camera id:
``accepted`` (the switch was made here; ``previous`` remembers the
detect_stream it replaced so reset can put it back; ``review_due`` after
``ACCEPT_REVIEW_DAYS``), or ``dismissed`` until a date, after which the
camera is evaluated afresh and re-proposed if it still qualifies. The
evaluation itself is computed on demand and cached briefly.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from services import site_settings
from services.detection_settings import normalize as normalize_detection

# ``models`` (and through it core.config) is imported inside the functions
# that need it: a module-level import would build Settings at import time,
# before a test environment exists (see tests/conftest.py).

#: camera id (str) -> {"status", "reason", "proposed_at", "accepted_at",
#: "dismissed_until", "previous", "by"}
STATE_KEY = "evidence_escalation"

WINDOW_HOURS = int(os.environ.get("OPENNVR_ESCALATION_WINDOW_HOURS", "24"))
MIN_VISITS = int(os.environ.get("OPENNVR_ESCALATION_MIN_VISITS", "20"))
FULL_RES_MIN = float(os.environ.get("OPENNVR_ESCALATION_FULL_RES_MIN", "0.5"))
SMALL_PX = int(os.environ.get("OPENNVR_ESCALATION_SMALL_PX", "48"))
SMALL_SHARE_MAX = float(os.environ.get("OPENNVR_ESCALATION_SMALL_SHARE_MAX", "0.5"))
#: How long a dismissal holds before the camera is looked at again.
DISMISS_DAYS_DEFAULT = 7
DISMISS_DAYS_MAX = 90
#: After this long on the main stream, the badge asks for a review: keep
#: or revert is the operator's call, since sub-stream quality cannot be
#: measured while the camera detects on main.
ACCEPT_REVIEW_DAYS = 7
#: The cost line shown with a proposal. A number measured on the box
#: (the hardware acceptance report) is better; this is the documented
#: ratio for a 1080p main / D1 sub pair.
COST_NOTE = "about 5x the sub stream's decode work, continuously, while it holds"

FULL_RES_SOURCES = frozenset({"main", "playback"})
_CACHE_TTL_S = 60.0


@dataclass
class Evaluation:
    camera_id: int
    window_hours: int
    visits: int                       # visits in the window with evidence facts
    full_res: int                     # of which the crop came from main / playback
    small: int                        # of which the subject was under SMALL_PX
    full_res_share: float | None
    small_share: float | None
    detect_stream: str                # the camera's setting: auto | sub | main
    qualifies: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["thresholds"] = {"min_visits": MIN_VISITS, "full_res_min": FULL_RES_MIN,
                           "small_px": SMALL_PX, "small_share_max": SMALL_SHARE_MAX}
        d["cost_note"] = COST_NOTE
        return d


_cache: dict[str, Any] = {"at": 0.0, "value": None}
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ── evaluation ──────────────────────────────────────────────────────

def evaluate_all(db: Session, *, now: datetime | None = None) -> dict[int, Evaluation]:
    """Every active camera's evaluation over the window, from the stored
    visits. One query for the fleet; aggregated in Python because the
    facts live in a JSON column and the window is a day."""
    from models import Camera, TimelineEvent

    now = now or _now()
    since = now - timedelta(hours=WINDOW_HOURS)
    cams = db.query(Camera.id, Camera.detection_settings).filter(Camera.deleted_at.is_(None)).all()
    streams = {cid: (normalize_detection(ds) or {}).get("detect_stream") or "auto" for cid, ds in cams}
    rows = (
        db.query(TimelineEvent.camera_id, TimelineEvent.payload)
        .filter(TimelineEvent.source == "tier0", TimelineEvent.event_type == "track",
                TimelineEvent.started_at >= since, TimelineEvent.payload.isnot(None))
        .all()
    )
    counts: dict[int, list[int]] = {}
    for cid, payload in rows:
        ev = (payload or {}).get("evidence") if isinstance(payload, dict) else None
        if not isinstance(ev, dict):
            continue
        c = counts.setdefault(cid, [0, 0, 0])
        c[0] += 1
        if str(ev.get("source") or "").lower() in FULL_RES_SOURCES:
            c[1] += 1
        box = ev.get("box_px")
        if isinstance(box, (int, float)) and box < SMALL_PX:
            c[2] += 1
    out: dict[int, Evaluation] = {}
    for cid, stream in streams.items():
        visits, full, small = counts.get(cid, [0, 0, 0])
        full_share = round(full / visits, 3) if visits else None
        small_share = round(small / visits, 3) if visits else None
        reasons: list[str] = []
        if visits >= MIN_VISITS and stream != "main":
            if full_share is not None and full_share < FULL_RES_MIN:
                reasons.append("low_res_evidence")
            if small_share is not None and small_share > SMALL_SHARE_MAX:
                reasons.append("small_subjects")
        out[cid] = Evaluation(camera_id=cid, window_hours=WINDOW_HOURS, visits=visits,
                              full_res=full, small=small, full_res_share=full_share,
                              small_share=small_share, detect_stream=stream,
                              qualifies=bool(reasons), reasons=reasons)
    return out


def evaluations(db: Session, *, fresh: bool = False) -> dict[int, Evaluation]:
    """``evaluate_all`` behind a short cache: the camera list asks on
    every page load."""
    with _lock:
        if not fresh and _cache["value"] is not None and time.monotonic() - _cache["at"] < _CACHE_TTL_S:
            return _cache["value"]
    value = evaluate_all(db)
    with _lock:
        _cache["at"], _cache["value"] = time.monotonic(), value
    return value


def invalidate() -> None:
    with _lock:
        _cache["at"], _cache["value"] = 0.0, None


# ── state ───────────────────────────────────────────────────────────

def _state(db: Session) -> dict[str, dict[str, Any]]:
    value = site_settings.get_json(db, STATE_KEY, {})
    return value if isinstance(value, dict) else {}


def _save(db: Session, state: dict[str, dict[str, Any]]) -> None:
    site_settings.set_json(db, STATE_KEY, state)


def status_for(db: Session, camera_id: int, ev: Evaluation | None, *,
               now: datetime | None = None) -> dict[str, Any] | None:
    """The camera's escalation record as the UI shows it, or None when
    there is nothing to show: not proposed, not accepted, not dismissed."""
    now = now or _now()
    rec = _state(db).get(str(camera_id))
    if rec:
        st = rec.get("status")
        if st == "accepted":
            acc = _parse(rec.get("accepted_at"))
            review_due = bool(acc and now - acc >= timedelta(days=ACCEPT_REVIEW_DAYS))
            return {**rec, "review_due": review_due}
        if st == "dismissed":
            until = _parse(rec.get("dismissed_until"))
            if until and now < until:
                return rec
            # the dismissal ran out: evaluated afresh below
    if ev is not None and ev.qualifies:
        return {"status": "proposed", "reasons": ev.reasons,
                "proposed_at": _iso(now), "cost_note": COST_NOTE}
    return None


def summary(db: Session, camera_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per camera, the record for the camera list's badge: ``{status,
    reasons, review_due}`` for proposed / accepted / dismissed cameras."""
    if not camera_ids:
        return {}
    evs = evaluations(db)
    out: dict[int, dict[str, Any]] = {}
    for cid in camera_ids:
        rec = status_for(db, cid, evs.get(cid))
        if rec:
            out[cid] = {"status": rec["status"], "reasons": rec.get("reasons") or [],
                        "review_due": bool(rec.get("review_due"))}
    return out


def view(db: Session, camera_id: int, *, fresh: bool = False) -> dict[str, Any]:
    """Everything the detection settings page shows for one camera."""
    evs = evaluations(db, fresh=fresh)
    ev = evs.get(camera_id)
    return {
        "camera_id": camera_id,
        "escalation": status_for(db, camera_id, ev),
        "evaluation": ev.as_dict() if ev else None,
    }


# ── decisions ───────────────────────────────────────────────────────

def _set_detect_stream(db: Session, camera: Any, value: str | None) -> None:
    """Change only ``detect_stream`` on the camera's detection settings,
    keeping every other override as it is."""
    current = dict(normalize_detection(camera.detection_settings) or {})
    if value is None:
        current.pop("detect_stream", None)
    else:
        current["detect_stream"] = value
    camera.detection_settings = current or None


def accept(db: Session, camera: Any, *, by: str) -> dict[str, Any]:
    """Switch the camera's detection to the main stream and remember what
    it replaced. The pipeline picks the change up on its next reconcile."""
    previous = (normalize_detection(camera.detection_settings) or {}).get("detect_stream")
    ev = evaluations(db).get(camera.id)
    _set_detect_stream(db, camera, "main")
    state = _state(db)
    state[str(camera.id)] = {
        "status": "accepted", "accepted_at": _iso(_now()), "by": by,
        "previous": previous, "reasons": ev.reasons if ev else [],
    }
    db.commit()
    _save(db, state)
    invalidate()
    return state[str(camera.id)]


def dismiss(db: Session, camera: Any, *, by: str, days: int = DISMISS_DAYS_DEFAULT) -> dict[str, Any]:
    """Put the proposal away for ``days``; the camera is looked at again after."""
    days = max(1, min(DISMISS_DAYS_MAX, int(days)))
    now = _now()
    state = _state(db)
    ev = evaluations(db).get(camera.id)
    state[str(camera.id)] = {
        "status": "dismissed", "dismissed_at": _iso(now),
        "dismissed_until": _iso(now + timedelta(days=days)), "by": by,
        "reasons": ev.reasons if ev else [],
    }
    _save(db, state)
    return state[str(camera.id)]


def reset(db: Session, camera: Any, *, by: str) -> dict[str, Any] | None:
    """Forget the decision. An accepted switch is undone: detect_stream goes
    back to what it was before the escalation."""
    state = _state(db)
    rec = state.pop(str(camera.id), None)
    if rec and rec.get("status") == "accepted":
        _set_detect_stream(db, camera, rec.get("previous"))
        db.commit()
    _save(db, state)
    invalidate()
    return rec
