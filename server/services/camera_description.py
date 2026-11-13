# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""One shared "what is on this camera now" (RFC-0004, step 3).

Two askers want *now* rather than the last visit: the camera agent
answering "what do you see on the gate camera", and the Mirror narrating
the scene in front of it. Both used to take their own frame and call the
caption model themselves — outside the enrichment gate, outside the
governor, and for the same frame the enrichment tier had just described
for the visit. Measured on a Ventuno Q (2026-11-12): those loops were a
third of the captioner's calls in a busy hour.

This is the one path for a live description:

1. **The store first.** The newest described visit on the camera is the
   answer when it is younger than ``max_age_s`` and no visit has started
   on the camera since it ended — nothing new to look at, so nothing to
   ask. Cost: one query.
2. **The last fresh look next.** A look made here for an earlier asker,
   under the same two conditions.
3. **Otherwise one gated call**, shared: the pipeline's newest decoded
   frame (no RTSP session of our own) goes to the captioner through the
   enrichment gate with the requested priority, so it pays into the same
   budget and yields to the detector like everything else. Every asker
   that arrives while that call is in flight gets the same answer
   (single-flight). When the camera has no fresh frame or the call is
   refused, the answer is the newest description stamped with its age
   and a reason — never an old frame presented as now.

**Which answer a live question gets.** A visit's caption describes the
best crop of ONE object — the man at the gate, not the gate. A person
asking "what do you see on the gate camera" wants the whole frame: the
two people, the parked car, the open door. So by default a live question
is answered from a look at the current full frame (shared and cached as
above), and the newest visit's caption is only the answer when the
caller says ``prefer_store=True`` — an app that wants "what was just
seen", a subtitle that follows the visits. Varun's rule (2026-11-13):
no optimisation may lower the quality of an answer; removing the
duplicate looks is the saving, narrowing the answer is not allowed.

What a live look is NOT: a visit. It is kept in memory per camera, not
written as an ``event_text`` row (there is no visit row for it), and the
visit's own description still arrives through the enrichment tier when
the track ends. The counters say how often each path answered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("camera_description")

#: How fresh the pipeline's decoded frame must be for a live look.
FRAME_MAX_AGE_S = 3.0

#: For the measurement: which path answered.
STATS: dict[str, int] = {"from_visit": 0, "from_fresh": 0, "model_calls": 0,
                         "shared": 0, "stale": 0}


@dataclass
class Fresh:
    text: str
    at: float                   # monotonic
    observed_at: datetime
    camera_id: int
    attributes: dict[str, str] = field(default_factory=dict)


_fresh: dict[int, Fresh] = {}
_inflight: dict[int, asyncio.Future] = {}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _newest_described_visit(db, camera_id: int) -> dict[str, Any] | None:
    """The camera's newest visit with a caption, its attributes, and
    whether any visit started on the camera after it ended."""
    from models import EventText, TimelineEvent, VisitDescriptor

    row = (db.query(TimelineEvent, EventText)
           .join(EventText, EventText.event_id == TimelineEvent.id)
           .filter(TimelineEvent.camera_id == camera_id, EventText.caption.isnot(None),
                   EventText.caption != "")
           .order_by(TimelineEvent.id.desc())
           .first())
    if row is None:
        return None
    visit, text = row
    ended = visit.ended_at or visit.started_at
    newer = (db.query(TimelineEvent.id)
             .filter(TimelineEvent.camera_id == camera_id, TimelineEvent.id > visit.id,
                     TimelineEvent.started_at > ended)
             .first()) is not None
    claims = db.query(VisitDescriptor).filter(VisitDescriptor.event_id == visit.id).all()
    return {"event_id": visit.id, "text": text.caption, "observed_at": ended,
            "attributes": {d.kind: d.value for d in claims if d.kind and d.value},
            "producer": text.source, "newer_visit": newer}


def _visit_started_after(db, camera_id: int, since: datetime) -> bool:
    from models import TimelineEvent

    return (db.query(TimelineEvent.id)
            .filter(TimelineEvent.camera_id == camera_id, TimelineEvent.started_at > since)
            .first()) is not None


async def _latest_frame(camera_id: int) -> bytes | None:
    """The pipeline's newest decoded frame for the camera, or None."""
    from core.config import settings

    base = (getattr(settings, "detect_pipeline_metrics_url", "") or "").rstrip("/")
    if not base:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            r = await client.get(f"{base}/latest_frame",
                                 params={"camera": f"cam{camera_id}", "max_age": f"{FRAME_MAX_AGE_S:g}"})
        if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
            return r.content
    except Exception:                              # noqa: BLE001
        logger.debug("camera description: no frame for cam%s", camera_id, exc_info=True)
    return None


async def _look(camera_id: int) -> tuple[str | None, str]:
    """One gated caption of the camera's current frame. (text, reason)."""
    from services.caption_enrichment import _caption_jpeg, _resolve_caption_adapter
    from services.enrichment_gate import DROPPED

    jpeg = await _latest_frame(camera_id)
    if jpeg is None:
        return None, "no fresh frame from the camera"
    adapter = await _resolve_caption_adapter()
    if adapter is None:
        return None, "no captioner registered"
    STATS["model_calls"] += 1
    text = await _caption_jpeg(jpeg, adapter, f"cam{camera_id}", priority="requested")
    if text is DROPPED:
        return None, "enrichment is yielding to the detector"
    if not text:
        return None, "the captioner did not answer"
    return str(text), "fresh look"


async def describe(camera_id: int, max_age_s: float = 30.0, *,
                   prefer_store: bool = False) -> dict[str, Any]:
    """See the module docstring. ``prefer_store`` lets the newest visit's
    caption answer when it is fresh and nothing newer has started; by
    default a live question gets a whole-frame look, shared and cached.
    Never raises on a bus or model failure; the answer then says what it
    is."""
    from core.database import SessionLocal

    now_mono = time.monotonic()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        newest = _newest_described_visit(db, camera_id)
        fresh = _fresh.get(camera_id)
        fresh_superseded = fresh is not None and _visit_started_after(db, camera_id, fresh.observed_at)
    finally:
        db.close()

    if prefer_store and newest is not None and not newest["newer_visit"]:
        age = (now - (newest["observed_at"].replace(tzinfo=UTC) if newest["observed_at"].tzinfo is None
                      else newest["observed_at"])).total_seconds()
        if 0 <= age <= max_age_s:
            STATS["from_visit"] += 1
            return {"camera_id": camera_id, "text": newest["text"], "source": "visit",
                    "event_id": newest["event_id"], "attributes": newest["attributes"],
                    "age_s": round(age, 1), "observed_at": _iso(newest["observed_at"]),
                    "model_called": False}
    if fresh is not None and not fresh_superseded and now_mono - fresh.at <= max_age_s:
        STATS["from_fresh"] += 1
        return {"camera_id": camera_id, "text": fresh.text, "source": "fresh",
                "age_s": round(now_mono - fresh.at, 1), "observed_at": _iso(fresh.observed_at),
                "attributes": fresh.attributes, "model_called": False}

    # One call, shared by everyone who asks while it runs.
    fut = _inflight.get(camera_id)
    if fut is not None:
        STATS["shared"] += 1
        text, reason = await asyncio.shield(fut)
    else:
        fut = asyncio.get_running_loop().create_future()
        _inflight[camera_id] = fut
        try:
            text, reason = await _look(camera_id)
            fut.set_result((text, reason))
        except Exception as exc:                   # noqa: BLE001
            fut.set_result((None, f"look failed: {type(exc).__name__}"))
            text, reason = None, f"look failed: {type(exc).__name__}"
        finally:
            _inflight.pop(camera_id, None)
    if text:
        _fresh[camera_id] = Fresh(text=text, at=time.monotonic(), observed_at=datetime.now(UTC),
                                  camera_id=camera_id)
        return {"camera_id": camera_id, "text": text, "source": "fresh", "age_s": 0.0,
                "observed_at": _iso(_fresh[camera_id].observed_at), "attributes": {},
                "model_called": True}

    # Nothing fresh could be had: the newest thing known, stamped honestly.
    STATS["stale"] += 1
    best = None
    if newest is not None:
        best = {"text": newest["text"], "observed_at": newest["observed_at"], "source": "visit",
                "event_id": newest["event_id"], "attributes": newest["attributes"]}
    if fresh is not None and (best is None or fresh.observed_at > (
            best["observed_at"].replace(tzinfo=UTC) if best["observed_at"].tzinfo is None else best["observed_at"])):
        best = {"text": fresh.text, "observed_at": fresh.observed_at, "source": "fresh", "attributes": fresh.attributes}
    if best is None:
        return {"camera_id": camera_id, "text": None, "source": "none", "age_s": None,
                "observed_at": None, "attributes": {}, "model_called": False, "reason": reason}
    obs = best["observed_at"]
    obs = obs.replace(tzinfo=UTC) if obs.tzinfo is None else obs
    return {"camera_id": camera_id, "text": best["text"], "source": "stale",
            "last_source": best["source"], "event_id": best.get("event_id"),
            "attributes": best.get("attributes") or {},
            "age_s": round((now - obs).total_seconds(), 1), "observed_at": _iso(obs),
            "model_called": False, "reason": reason}


def reset_for_tests() -> None:
    _fresh.clear()
    _inflight.clear()
    for k in STATS:
        STATS[k] = 0
