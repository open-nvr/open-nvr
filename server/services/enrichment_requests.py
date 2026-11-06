# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Describe what a question needs, now — the requested lane (#583).

A search for "blue car on the gate camera between two and four" can be
answered only from visits somebody has already described. On a box
where enrichment is off (a CPU install — the installer's default), or
throttled, or simply behind, most of those visits have no caption and
no colour claim yet. Search answers from what exists and says so; this
module is the "and I'm describing the rest now" that goes with it.

Three parts:

* :func:`missing_for` — of the visits the question's STRUCTURAL filters
  match (label, camera, window, plate, zone), which lack the parameter
  the question needs: a caption when there are words to match, a claim
  of the kind an attribute asks for. Newest first, capped.
* :func:`request` — queue those for enrichment, deduplicated against
  what is already queued or in flight, and return a ticket the caller
  can put in its answer: how many, and roughly how long.
* :func:`run_requests_worker` — one worker, started at boot, that hands
  each visit to the SAME enrichers the ingest path calls, with the
  ``requested`` priority: the governor lets it through in LIVE-ONLY
  with a full wait line (somebody is waiting on this, unlike a live
  visit the catch-up will reach) and still refuses it PAUSED (the box
  is losing; a question does not change that). Each finished request
  is announced on the bus so the agent or the UI can ask again.

Bounded on purpose: ``EVENTS_ENRICHMENT_REQUEST_CAP`` visits per
question. "Last month" on a busy site is thousands; the answer to that
is a narrower window, not a queue that runs until tomorrow.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func

from models import EventText, TimelineEvent, VisitDescriptor

logger = logging.getLogger("enrichment_requests")

#: The bus event a finished request publishes.
DONE_ALERT_TYPE = "enrichment_request_done"

#: Per-call estimate when no adapter has answered yet this process.
DEFAULT_CALL_S = 5.0

#: How long the worker waits between re-checks while the governor says
#: PAUSED, and how many times a visit refused by the gate is retried.
_PAUSED_POLL_S = 5.0
_DROP_RETRIES = 3
_DROP_RETRY_S = 2.0


def request_cap() -> int:
    from core.config import settings

    return max(1, int(getattr(settings, "events_enrichment_request_cap", 50)))


# ── what is missing ───────────────────────────────────────────────

#: How many of the newest matching visits are examined for a question.
#: The count reported is over this window — "at least N", never a
#: table scan — and the cap decides how many of them one request takes.
WINDOW = 200

#: A visit younger than this is the LIVE path's, still: its caption and
#: VQA tasks were spawned at ingest and may be at the gate or on the
#: wire (admission wait plus the call's own deadline). Queuing it here
#: too would run the same inference twice and let the second write race
#: the first. Older than this and still undescribed, the live path is
#: done with it — dropped, refused, or off — and the lane may have it.
#: Short on purpose, unlike the catch-up's ten minutes: a person is
#: waiting on this one.
LIVE_GRACE_S = 120.0


def _in_caption_cooldown(failed_at: Any) -> bool:
    """The last caption call for this visit failed less than
    events_caption_retry_cooldown_s ago: not missing a caption for now."""
    if not failed_at:
        return False
    from datetime import UTC, datetime
    from core.config import settings
    cooldown = float(getattr(settings, "events_caption_retry_cooldown_s", 600.0) or 0)
    if cooldown <= 0:
        return False
    try:
        t = datetime.fromisoformat(str(failed_at))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return (datetime.now(UTC) - t).total_seconds() < cooldown


def missing_for(db, *, filters: dict[str, Any], labels: list[str] | None,
                camera_ids: list[int] | None, want_caption: bool,
                want_kinds: set[str], cap: int | None = None) -> dict[str, Any]:
    """Visits the structural filters match that lack what the question
    needs — decided per row EXACTLY as the enrichers will decide, so the
    lane never promises "describing them now" about a visit the enricher
    is going to refuse:

    * the per-camera skill assignment (``wants_caption`` /
      ``wants_descriptors``: "no assignment, no caption, no cost");
    * the people opt-in for descriptors (``events_descriptor_people``);
    * only the kinds asked of THIS class (``LABEL_KINDS``: a lorry is not
      asked what colour its top is);
    * "looked and found nothing" markers (``enriched_by``) — a visit the
      captioner or VQA already looked at is not missing anything.

    ``filters`` are ``timeline_service._events_query`` keywords — the
    same predicate search pages with. Returns ``{"ids": [...newest
    first, capped], "needs": {id: {"caption": bool, "descriptors":
    bool}}, "caption": n, "kinds": {kind: n}, "total": n, "window": w}``
    with the counts over the newest ``WINDOW`` matching rows.
    """
    from core.config import settings
    from models import Camera
    from services.caption_enrichment import (
        CAPTION_FAILED_AT,
        CAPTION_SKILL,
        CAPTIONABLE_LABELS,
        wants_caption,
    )
    from services.descriptor_enrichment import (
        DESCRIBABLE_LABELS,
        LABEL_KINDS,
        PEOPLE_SETTING,
        VQA_TASK,
        wants_descriptors,
    )
    from services.skill_assignments import camera_skills
    from services.timeline_service import _events_query

    cap = cap or request_cap()
    out: dict[str, Any] = {"ids": [], "needs": {}, "caption": 0, "kinds": {},
                           "total": 0, "window": WINDOW}
    if not (want_caption or want_kinds):
        return out
    people = bool(getattr(settings, PEOPLE_SETTING, False))
    interesting = set()
    if want_caption:
        interesting |= CAPTIONABLE_LABELS
    if want_kinds:
        interesting |= DESCRIBABLE_LABELS
    if labels:
        interesting &= {str(x).lower() for x in labels}
    if not interesting:
        return out

    settled = datetime.now(UTC) - timedelta(seconds=LIVE_GRACE_S)
    q = (_events_query(db, **filters)
         .join(Camera, Camera.id == TimelineEvent.camera_id)
         .filter(TimelineEvent.evidence_path.isnot(None))
         .filter(TimelineEvent.label.in_(sorted(interesting)))
         .filter(func.coalesce(TimelineEvent.observed_at, TimelineEvent.started_at) <= settled))
    if camera_ids:
        q = q.filter(TimelineEvent.camera_id.in_([int(c) for c in camera_ids]))
    rows = (q.with_entities(TimelineEvent, Camera)
             .order_by(TimelineEvent.id.desc()).limit(WINDOW).all())
    if not rows:
        return out

    ids = [int(r.id) for r, _ in rows]
    captioned = {
        e for (e,) in db.query(EventText.event_id)
        .filter(EventText.event_id.in_(ids), EventText.caption.isnot(None),
                EventText.caption != "").all()
    }
    claimed: dict[int, set[str]] = {}
    for e, k in db.query(VisitDescriptor.event_id, VisitDescriptor.kind).filter(
            VisitDescriptor.event_id.in_(ids)).all():
        claimed.setdefault(int(e), set()).add(str(k))

    kinds_missing: dict[str, int] = {k: 0 for k in sorted(want_kinds)}
    for row, camera in rows:
        rid = int(row.id)
        label = (row.label or "").lower()
        skills = camera_skills(camera)
        looked = set((row.payload or {}).get("enriched_by") or [])
        # The assignment is checked HERE before the enricher's predicate
        # is asked: wants_caption counts an unassigned camera toward the
        # ingest path's "N qualifying visits skipped" warning, and this is
        # a search, not an ingest — a page of results must not read as a
        # thousand skipped visits in the log.
        need_caption = bool(
            want_caption and rid not in captioned and CAPTION_SKILL not in looked
            and CAPTION_SKILL in skills
            and not _in_caption_cooldown((row.payload or {}).get(CAPTION_FAILED_AT))
            and wants_caption(row.label, row.evidence_path, True, skills))
        askable = [k for k in want_kinds if k in LABEL_KINDS.get(label, ())]
        need_kinds = [
            k for k in askable if k not in claimed.get(rid, set())
        ] if (askable and VQA_TASK not in looked
              and wants_descriptors(row.label, row.evidence_path, True, skills, people)) else []
        if not (need_caption or need_kinds):
            continue
        out["total"] += 1
        if need_caption:
            out["caption"] += 1
        for k in need_kinds:
            kinds_missing[k] = kinds_missing.get(k, 0) + 1
        if len(out["ids"]) < cap:
            out["ids"].append(rid)
            out["needs"][rid] = {"caption": need_caption, "descriptors": bool(need_kinds)}
    out["kinds"] = {k: n for k, n in kinds_missing.items() if n}
    return out


# ── from a question to a ticket ───────────────────────────────────

async def box_offers() -> tuple[bool, set[str]]:
    """(a captioner is registered and healthy, the descriptor kinds a
    healthy VQA skill offers). Best-effort: an unreadable plan reads as
    "offers nothing", which queues nothing — never an error."""
    try:
        from services.enrichment_plan import compute_enrichment_plan

        plan = await compute_enrichment_plan(None)
    except Exception:                              # noqa: BLE001
        return False, set()
    captions, kinds = False, set()
    for s in (plan or {}).get("skills") or []:
        if not isinstance(s, dict) or not s.get("healthy"):
            continue
        if s.get("task") == "image_captioning":
            captions = True
        kinds.update(k for k in (s.get("descriptor_kinds") or []) if isinstance(k, str))
    kinds.discard("face_id")                       # stays with the app that owns it
    return captions, kinds


def kinds_for_attrs(attrs) -> set[str]:
    """The descriptor kinds a list of (kind, value) chips asks for. A
    bare value (kind None — "blue") is resolved through the enricher's
    own vocabulary, so it asks for exactly the kinds that could have
    said it."""
    from services.descriptor_enrichment import KIND_QUESTIONS, normalise_answer

    out: set[str] = set()
    for kind, value in attrs or []:
        if kind:
            out.add(str(kind))
            continue
        for k in KIND_QUESTIONS:
            if normalise_answer(k, str(value)):
                out.add(k)
    return out


async def pending_for(db, *, filters: dict[str, Any], labels, camera_ids,
                      want_caption: bool, want_kinds: set[str],
                      offers: tuple[bool, set[str]] | None = None) -> dict[str, Any] | None:
    """The block a search answer carries: what is not described yet, and
    what this call queued. None when the question needs nothing the box
    could produce — a plain label-and-time search says nothing here.
    ``offers`` is (captioner healthy, kinds offered) when the caller has
    already read the plan (search resolves its needs from it); otherwise
    it is read here."""
    captions_ok, offered = offers if offers is not None else await box_offers()
    want_caption = bool(want_caption and captions_ok)
    want_kinds = {k for k in want_kinds if k in offered}
    if not (want_caption or want_kinds):
        return None
    missing = missing_for(db, filters=filters, labels=labels, camera_ids=camera_ids,
                          want_caption=want_caption, want_kinds=want_kinds)
    needs_block = {"caption": want_caption, "kinds": sorted(want_kinds)}
    if not missing["ids"]:
        return {"missing": 0, "requested": 0, "already_queued": 0, "eta_s": 0.0,
                "request_id": None, "needs": needs_block}
    ticket = request(missing["ids"], needs=missing["needs"])
    return {"missing": int(missing["total"]), "window": missing["window"],
            "capped_at": request_cap(), **ticket, "needs": needs_block}


# ── the queue ─────────────────────────────────────────────────────

STEPS = ("caption", "descriptors")


@dataclass
class Request:
    request_id: str
    #: visit id → the steps THIS request will run for it
    steps: dict[int, set[str]]
    created_at: float = field(default_factory=time.monotonic)
    described: int = 0
    dropped: int = 0

    @property
    def event_ids(self) -> list[int]:
        return list(self.steps)


_queue: asyncio.Queue[Request] | None = None
#: visit id → steps queued or in flight for it, across all requests
_inflight: dict[int, set[str]] = {}
_pending_calls = 0                   # calls still ahead in the queue


def _q() -> asyncio.Queue[Request]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def _calls(steps: set[str]) -> int:
    return (1 if "caption" in steps else 0) + (2 if "descriptors" in steps else 0)


def per_call_estimate() -> float:
    """Seconds per adapter call, from the slowest gate that has answered
    recently; the default before anything has."""
    from services.enrichment_gate import snapshot_all
    from services.enrichment_governor import SLOW_SIGNAL_TTL_S

    best = None
    for snap in snapshot_all():
        lat, age = snap.get("latency_ewma_s"), snap.get("latency_age_s")
        if lat is None or (age is not None and age > SLOW_SIGNAL_TTL_S):
            continue
        best = lat if best is None else max(best, lat)
    return float(best) if best is not None else DEFAULT_CALL_S


def request(event_ids: list[int], *, needs: dict[int, dict[str, bool]] | None = None,
            caption: bool = False, descriptors: bool = False) -> dict[str, Any]:
    """Queue visits for enrichment; return the ticket for the answer.

    ``needs`` says per visit which steps it lacks (``missing_for`` builds
    it); without it every visit gets ``caption``/``descriptors``. A step
    already queued or in flight for a visit — from this question or an
    earlier one — is not queued twice; a step that is NOT yet queued is,
    even for a visit another question already holds (a caption-only
    request must not swallow a later question's colour claim).

    ``{"request_id", "requested": n, "already_queued": m, "eta_s": s}`` —
    ``requested`` counts visits this call added work for.
    """
    global _pending_calls
    steps: dict[int, set[str]] = {}
    already = 0
    for raw in event_ids:
        eid = int(raw)
        want = ({k for k in STEPS if (needs.get(eid) or {}).get(k)} if needs is not None
                else {k for k, on in (("caption", caption), ("descriptors", descriptors)) if on})
        fresh = want - _inflight.get(eid, set())
        if not want:
            continue
        if not fresh:
            already += 1
            continue
        steps[eid] = fresh
    if not steps:
        eta = _pending_calls * per_call_estimate()
        return {"request_id": None, "requested": 0, "already_queued": already,
                "eta_s": round(eta, 1)}
    req = Request(request_id=uuid.uuid4().hex[:12], steps=steps)
    for eid, st in steps.items():
        _inflight.setdefault(eid, set()).update(st)
        _pending_calls += _calls(st)
    _q().put_nowait(req)
    eta = _pending_calls * per_call_estimate()
    logger.info("enrichment requests: %s queued %d visit(s) (%s), ~%.0fs",
                req.request_id, len(steps),
                ", ".join(sorted({k for st in steps.values() for k in st})), eta)
    return {"request_id": req.request_id, "requested": len(steps),
            "already_queued": already, "eta_s": round(eta, 1)}


def queue_depth() -> int:
    return len(_inflight)


# ── the worker ────────────────────────────────────────────────────

async def _wait_while_paused() -> None:
    from services.enrichment_governor import State, governor

    said = False
    while governor().evaluate() is State.PAUSED:
        if not said:
            logger.info("enrichment requests: holding — %s", governor().reason)
            said = True
        await asyncio.sleep(_PAUSED_POLL_S)


async def _one(event_id: int, req: Request) -> None:
    global _pending_calls
    from services.caption_enrichment import enrich_event_caption
    from services.descriptor_enrichment import enrich_event_descriptors

    fns = {"caption": enrich_event_caption, "descriptors": enrich_event_descriptors}
    ran_any = False
    for name in STEPS:
        if name not in req.steps.get(event_id, set()):
            continue
        try:
            res = None
            for attempt in range(_DROP_RETRIES + 1):
                await _wait_while_paused()
                try:
                    res = await fns[name](event_id, priority="requested")
                except Exception:                  # noqa: BLE001
                    logger.warning("enrichment requests: %s failed for %s", name,
                                   event_id, exc_info=True)
                    res = None
                if res != "dropped":
                    break
                if attempt < _DROP_RETRIES:
                    await asyncio.sleep(_DROP_RETRY_S)
            if res == "dropped":
                req.dropped += 1
            elif res == "done":
                # The enricher LOOKED (wrote, or marked "nothing to say").
                # None is every early return — row gone, no adapter, an
                # unreadable frame — and is not "described".
                ran_any = True
        finally:
            # Whatever happened — done, given up, cancelled — this call is
            # no longer ahead of anyone: the ETA must not carry it forever.
            _pending_calls = max(0, _pending_calls - _calls({name}))
            _inflight.get(event_id, set()).discard(name)
            if not _inflight.get(event_id):
                _inflight.pop(event_id, None)
    if ran_any:
        req.described += 1


async def _announce(req: Request, *, cancelled: bool = False) -> None:
    payload = {"request_id": req.request_id, "visits": len(req.steps),
               "described": req.described, "dropped": req.dropped,
               "cancelled": cancelled,
               "took_s": round(time.monotonic() - req.created_at, 1)}
    logger.info("enrichment requests: %s done — %s", req.request_id, payload)
    try:
        from services.event_bus_service import publish_system_alert

        await publish_system_alert(alert_type=DONE_ALERT_TYPE, state=None,
                                   severity="info", payload=payload)
    except Exception:                              # noqa: BLE001
        logger.debug("enrichment requests: bus publish failed", exc_info=True)


async def process_one_request(req: Request) -> None:
    global _pending_calls
    cancelled = False
    try:
        for event_id in req.event_ids:
            await _one(event_id, req)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        for event_id, st in req.steps.items():
            # Cancelled mid-way: release what this request still held —
            # the visits, and the calls never made, which are no longer
            # ahead of anyone's estimate.
            left = _inflight.get(event_id)
            if left:
                unrun = left & st
                _pending_calls = max(0, _pending_calls - _calls(unrun))
                left -= st
                if not left:
                    _inflight.pop(event_id, None)
        # Announced either way — a listener waiting on this id must not
        # wait forever — but an aborted request says so, not "done".
        await _announce(req, cancelled=cancelled)


async def run_requests_worker() -> None:
    """Forever: take the next request, describe its visits in order."""
    logger.info("enrichment requests: worker started (cap %d per question)",
                request_cap())
    while True:
        req = await _q().get()
        try:
            await process_one_request(req)
        except asyncio.CancelledError:
            raise
        except Exception:                          # noqa: BLE001
            logger.warning("enrichment requests: request %s failed",
                           req.request_id, exc_info=True)


def _reset_for_tests() -> None:
    global _queue, _pending_calls
    _queue = None
    _inflight.clear()
    _pending_calls = 0
