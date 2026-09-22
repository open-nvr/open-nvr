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

"""Following one object across cameras — the route it took.

A visit's ``track_id`` belongs to one camera: Tier-0 assigns it inside a
single stream, and nothing joins camera A's track 312 to camera B's track
88. That join is what a route question needs, and there are exactly two
ways to make it.

**The certain way: an exact identity.** A plate read by the LPR adapter,
a face recognised by the face adapter — both served through KAI-C, both
landing here as a descriptor — is the same string on two cameras, and the
same string is the same object. No inference, no score, no argument.

**The general way: evidence.** Most objects have no exact identity. A
person in a crowd, an unplated van, a trolley. For those the question is
answered by combining what is known, and the three ingredients are:

* **Where it could have gone**, from :class:`CameraTransition` — a graph
  learned from the certain journeys above, so the search looks at the
  cameras that actually follow this one, in the time such a trip takes.
* **What the skills said**, from the descriptors. Whatever KAI-C had
  registered and healthy when the visit was enriched: colour, vehicle
  type, clothing, carrying. The MORE skills a deployment runs, the more
  of this there is, and the sharper the answer — which is the whole
  argument for enriching at capture time.
* **How surprising each agreement is.** Two red objects agreeing on
  "red" is worth little at a depot where half the fleet is red, and a
  great deal where one van is. That is measured from the store rather
  than assumed, per kind and value.

Three rules keep it honest, and each exists because ignoring it is how
this kind of matching produces confident nonsense:

1. **Missing is not mismatch.** A descriptor only counts when BOTH
   visits have it. A visit nobody enriched must score neutrally, not
   badly, or adding a skill would make recall worse.
2. **Nothing is decided on one weak agreement.** Colour alone is the
   least stable attribute across cameras — white balance, IR at night,
   sun and shade — so it contributes, and cannot by itself carry a hop.
3. **Every hop shows its working.** The score comes back with the
   reasons that produced it, including the ones that argued against.
   A route an operator cannot audit is not evidence, and this is
   ultimately used to say where somebody was.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func

from models import CameraTransition, TimelineEvent, VisitDescriptor
from services.timeline_service import _events_query

__all__ = [
    "ANCHOR_KINDS", "Hop", "Journey", "learn_transitions", "find_journey",
]

#: Descriptor kinds that ARE an identity rather than evidence of one.
#: Both come from KAI-C adapters; both are exact-match.
#:
#: Only ``plate`` has a producer today. Nothing in this repository writes
#: a ``face_id`` claim — core refuses to, leaving a name on a person to
#: an app the operator installed on purpose, and no app writes one
#: either. The face branches below are therefore correct, priced, and
#: never taken, and a journey whose only link is a recognised face
#: cannot form. That is a decision rather than an oversight;
#: ``tests/test_descriptor_producers.py`` carries the reasoning and
#: fails if it stops being true.
ANCHOR_KINDS = ("plate", "face_id")

#: How much each kind of agreement is worth before its surprise is taken
#: into account. Colour is deliberately the weakest: it is the attribute
#: that changes most between two cameras looking at the same object.
KIND_WEIGHT: dict[str, float] = {
    "plate": 4.0,
    "face_id": 4.0,
    "vehicle_type": 1.4,
    "clothing_top": 1.2,
    "carrying": 1.0,
    "colour": 0.7,
}
DEFAULT_WEIGHT = 0.8

#: A disagreement counts for less than an agreement of the same kind,
#: because one of the two skills may simply have been wrong — a bad read
#: should cost a candidate, not eliminate it.
DISAGREE_SCALE = 0.6

#: With no learned edge, how long a trip between two cameras may take
#: before it stops being plausible. Wide on purpose: an unlearned edge
#: means "we do not know yet", not "impossible".
DEFAULT_MAX_TRANSIT = 15 * 60.0


@dataclass
class Hop:
    """One step of a route: a candidate visit and why it is believed."""

    event: TimelineEvent
    score: float
    transit_seconds: float
    method: str                       # identity | evidence | time-only
    why: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event.id,
            "camera_id": self.event.camera_id,
            "label": self.event.label,
            "started_at": self.event.started_at.isoformat() if self.event.started_at else None,
            "ended_at": self.event.ended_at.isoformat() if self.event.ended_at else None,
            "evidence_url": (
                f"/api/v1/events/{self.event.id}/evidence" if self.event.evidence_path else None
            ),
            "anchor": {
                "camera_id": self.event.camera_id,
                "at": self.event.started_at.isoformat() if self.event.started_at else None,
            },
            "transit_seconds": round(self.transit_seconds, 1),
            "score": round(self.score, 3),
            "method": self.method,
            "why": self.why,
        }


@dataclass
class Journey:
    """An anchor visit and the route believed to follow from it."""

    anchor: TimelineEvent
    hops: list[Hop]
    method: str
    caveat: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchor": {
                "event_id": self.anchor.id,
                "camera_id": self.anchor.camera_id,
                "label": self.anchor.label,
                "started_at": (
                    self.anchor.started_at.isoformat() if self.anchor.started_at else None
                ),
                # Same shape as a hop's. Without it a client cannot tell
                # "no frame was kept" from "the frame failed to load",
                # and renders a broken image for the first stop of every
                # route whose evidence has aged out.
                "evidence_url": (
                    f"/api/v1/events/{self.anchor.id}/evidence"
                    if self.anchor.evidence_path else None
                ),
            },
            "method": self.method,
            "caveat": self.caveat,
            "hops": [h.as_dict() for h in self.hops],
        }


# ── descriptors ──────────────────────────────────────────────────────


def _claims(db, event_ids: list[int]) -> dict[int, dict[str, VisitDescriptor]]:
    """Claims per visit, best-confidence-wins per kind.

    Two skills may disagree about the same kind. For matching, the more
    confident claim is used and the disagreement is left visible in the
    store — resolving it here would hide which skill was wrong.
    """
    out: dict[int, dict[str, VisitDescriptor]] = {}
    if not event_ids:
        return out
    rows = db.query(VisitDescriptor).filter(VisitDescriptor.event_id.in_(event_ids)).all()
    for d in rows:
        best = out.setdefault(d.event_id, {})
        prev = best.get(d.kind)
        if prev is None or (d.confidence or 0) > (prev.confidence or 0):
            best[d.kind] = d
    return out


def _surprise(db, kind: str, value: str, cache: dict[tuple[str, str], float]) -> float:
    """How much an agreement on this value is worth here, 0.2 … 2.0.

    A value shared by most of what this deployment sees says almost
    nothing; a rare one says a great deal. Measured, because "red" means
    something different at a fire station and at a car park.
    """
    key = (kind, value)
    if key in cache:
        return cache[key]
    total = db.query(func.count(VisitDescriptor.id)).filter(
        VisitDescriptor.kind == kind).scalar() or 0
    seen = db.query(func.count(VisitDescriptor.id)).filter(
        VisitDescriptor.kind == kind, VisitDescriptor.value == value).scalar() or 0
    if total <= 0 or seen <= 0:
        weight = 1.0
    else:
        # log(N / n) normalised into a band: never zero (an agreement is
        # always worth something) and never runaway (a value seen once
        # must not outweigh a plate).
        weight = max(0.2, min(2.0, math.log((total + 1) / seen) / math.log(10) + 0.4))
    cache[key] = weight
    return weight


# ── topology ─────────────────────────────────────────────────────────


def learn_transitions(db, *, since: datetime | None = None, max_gap_seconds: float = 900.0) -> int:
    """Learn the camera graph from journeys we are certain about.

    Certainty comes from an exact identity — a plate from the LPR
    adapter, a face from the face adapter, both through KAI-C. Every time
    the same identity appears on camera A and then on camera B within
    ``max_gap_seconds``, that is one observed trip.

    Returns the number of edges written. Cheap enough to run nightly; the
    graph only changes when the site does.

    A FULL relearn (``since=None``) is authoritative: it also removes
    edges nothing supports any more. The site's topology is not
    append-only — this table exists because a route "changes when a gate
    is closed or a camera is re-aimed" — and an edge that only ever
    accumulates would keep asserting a route that no longer exists,
    which ``_transit_fit`` would then score as plausible. An edge
    therefore survives exactly as long as evidence for it survives,
    which is the rule the rest of this store follows.

    With one exception, and it is the usual one: a scan that finds NO
    trips at all prunes nothing. Zero anchors means LPR is off, an
    adapter is down, or the database is new — "could not check", not
    "no route exists" — and those are different answers.

    ``since`` narrows the scan to a WINDOW, and the counts it writes are
    the window's, not a running total. It is for asking "what did the
    graph look like last week", never for incremental accumulation: a
    nightly ``since=yesterday`` would overwrite every edge's samples
    with one day's worth. It does not prune, for the same reason — a
    window cannot speak for what it did not look at.
    """
    q = (
        db.query(VisitDescriptor.kind, VisitDescriptor.value,
                 TimelineEvent.camera_id, TimelineEvent.started_at)
        .join(TimelineEvent, TimelineEvent.id == VisitDescriptor.event_id)
        .filter(VisitDescriptor.kind.in_(ANCHOR_KINDS))
    )
    if since is not None:
        q = q.filter(TimelineEvent.started_at >= since)
    rows = q.order_by(VisitDescriptor.kind, VisitDescriptor.value,
                      TimelineEvent.started_at).all()

    trips: dict[tuple[int, int], list[float]] = {}
    prev_key: tuple[str, str] | None = None
    prev_cam: int | None = None
    prev_at: datetime | None = None
    for kind, value, camera_id, started_at in rows:
        key = (kind, value)
        if key == prev_key and prev_cam is not None and prev_at is not None:
            gap = (started_at - prev_at).total_seconds()
            # Same camera again is the object still there, not a trip;
            # a gap beyond the window is two separate visits, not one
            # journey, and pretending otherwise invents edges.
            if camera_id != prev_cam and 0 < gap <= max_gap_seconds:
                trips.setdefault((prev_cam, camera_id), []).append(gap)
        prev_key, prev_cam, prev_at = key, camera_id, started_at

    written = 0
    for (a, b), gaps in trips.items():
        gaps.sort()
        median = gaps[len(gaps) // 2]
        p90 = gaps[min(len(gaps) - 1, int(len(gaps) * 0.9))]
        edge = (
            db.query(CameraTransition)
            .filter(CameraTransition.from_camera_id == a, CameraTransition.to_camera_id == b)
            .one_or_none()
        )
        if edge is None:
            edge = CameraTransition(from_camera_id=a, to_camera_id=b)
            db.add(edge)
        edge.samples = len(gaps)
        edge.median_seconds = median
        edge.p90_seconds = p90
        # Set explicitly: the column has a server default but no
        # onupdate, so without this a relearned edge keeps reading as
        # first-seen and nothing can tell a live route from a fossil.
        edge.updated_at = datetime.now(UTC)
        written += 1

    if since is None and trips:
        # Only a full scan may prune — see the docstring. And only a scan
        # that SAW something: finding no trips at all means the anchors
        # are missing (LPR switched off, an adapter down, a fresh
        # database), which is "could not check", not "no route exists".
        # Wiping the graph on that reading is the same mistake as
        # treating an unenriched visit as a mismatch.
        for edge in db.query(CameraTransition).all():
            if (edge.from_camera_id, edge.to_camera_id) not in trips:
                db.delete(edge)
    db.commit()
    return written


def _edges_from(db, camera_id: int) -> dict[int, CameraTransition]:
    return {
        e.to_camera_id: e
        for e in db.query(CameraTransition).filter(
            CameraTransition.from_camera_id == camera_id).all()
    }


def _transit_fit(edge: CameraTransition | None, gap: float) -> tuple[float, str]:
    """How well this gap fits the trip, and how to say so in words."""
    if edge is None or not edge.median_seconds:
        return 0.0, "no learned route between these cameras yet"
    if edge.samples < 3:
        return 0.15, f"route seen only {edge.samples}x — weak evidence"
    span = max(edge.p90_seconds or edge.median_seconds, edge.median_seconds) or 1.0
    if gap <= span:
        return 1.0, (f"arrives in {int(gap)}s, normal for this route "
                     f"(~{int(edge.median_seconds)}s, {edge.samples} trips)")
    # Late, but late is not impossible — somebody stopped on the way.
    over = gap / span
    return max(0.0, 1.5 - over), f"{int(gap)}s is slow for this route (~{int(span)}s)"


# ── the route ────────────────────────────────────────────────────────


def _score_pair(db, a_claims: dict[str, VisitDescriptor], b_claims: dict[str, VisitDescriptor],
                cache: dict[tuple[str, str], float]) -> tuple[float, list[str], bool]:
    """Evidence for two visits being the same object.

    Returns (score, reasons, is_identity). Only kinds BOTH sides carry
    count: a claim the other visit was never asked about says nothing
    about whether they match.
    """
    for kind in ANCHOR_KINDS:
        a, b = a_claims.get(kind), b_claims.get(kind)
        if a and b and a.value == b.value:
            return 1.0, [f"same {kind} ({a.value}) — an exact identity, not an inference"], True

    agree = 0.0
    against = 0.0
    reasons: list[str] = []
    shared = 0
    for kind, a in a_claims.items():
        b = b_claims.get(kind)
        if b is None:
            continue                      # nobody looked — not a mismatch
        shared += 1
        weight = KIND_WEIGHT.get(kind, DEFAULT_WEIGHT)
        conf = (a.confidence or 0.7) * (b.confidence or 0.7)
        if a.value == b.value:
            surprise = _surprise(db, kind, a.value, cache)
            agree += weight * conf * surprise
            reasons.append(
                f"{kind} matches ({a.value})"
                + (" — common here, so weak evidence" if surprise < 0.6 else "")
                + (" — rare here, so strong evidence" if surprise > 1.3 else "")
            )
        else:
            against += weight * conf * DISAGREE_SCALE
            reasons.append(f"{kind} differs ({a.value} vs {b.value})")

    if shared == 0:
        return 0.0, ["nothing was known about both of these to compare"], False
    net = agree - against
    # A single weak agreement must not read as a match: one shared
    # colour is a coincidence at any site with more than a few objects.
    if shared == 1 and net < 1.0:
        net *= 0.5
        reasons.append("only one attribute in common — treated as weak")
    return 1 / (1 + math.exp(-(net - 1.0))), reasons, False


def find_journey(
    db,
    *,
    event_id: int,
    scope: set[int] | None,
    window_minutes: float = 30.0,
    max_hops: int = 6,
    min_score: float = 0.35,
) -> Journey | None:
    """Follow the object in ``event_id`` forward across cameras.

    Greedy: from the anchor, the best-scoring plausible next visit
    becomes the next anchor. Greedy rather than a full search on
    purpose — an operator reads a route as a sequence and corrects it
    hop by hop, and a beam search that quietly re-writes earlier hops
    when a later one scores well is much harder to trust.
    """
    anchor = db.get(TimelineEvent, int(event_id))
    if anchor is None or (scope is not None and anchor.camera_id not in scope):
        return None

    cache: dict[tuple[str, str], float] = {}
    hops: list[Hop] = []
    seen_ids = {anchor.id}
    current = anchor
    methods: set[str] = set()

    for _ in range(max(1, min(12, max_hops))):
        current_claims = _claims(db, [current.id]).get(current.id, {})
        edges = _edges_from(db, current.camera_id)
        start = current.ended_at or current.started_at
        if start is None:
            break
        window_end = start + timedelta(minutes=window_minutes)

        q = _events_query(
            db, from_=start, to=window_end, scope=scope,
            label=current.label,
        ).filter(TimelineEvent.camera_id != current.camera_id,
                 TimelineEvent.id.notin_(seen_ids))
        candidates = q.order_by(TimelineEvent.started_at.asc()).limit(200).all()
        if not candidates:
            break

        claims = _claims(db, [c.id for c in candidates])
        best: Hop | None = None
        for cand in candidates:
            gap = ((cand.started_at - start).total_seconds()
                   if cand.started_at and start else 0.0)
            if gap < 0:
                continue
            edge = edges.get(cand.camera_id)
            fit, fit_why = _transit_fit(edge, gap)
            if edge is None and gap > DEFAULT_MAX_TRANSIT:
                continue
            ev, why, identity = _score_pair(db, current_claims, claims.get(cand.id, {}), cache)
            if identity:
                score, method = 1.0, "identity"
            else:
                # Evidence and plausibility both matter: the right-looking
                # object at an impossible time is not the same object, and
                # the right time alone is not an identification.
                prior = 0.35 + 0.65 * fit
                score = ev * prior
                method = "evidence" if ev > 0 else "time-only"
                why = why + [fit_why]
            if score >= min_score and (best is None or score > best.score):
                best = Hop(event=cand, score=score, transit_seconds=gap,
                           method=method, why=why)
        if best is None:
            break
        hops.append(best)
        methods.add(best.method)
        seen_ids.add(best.event.id)
        current = best.event

    method = ("identity" if methods == {"identity"}
              else "evidence" if "evidence" in methods
              else "time-only" if methods else "none")
    caveat = {
        "identity": "",
        "evidence": "Hops after the first are inferred from what the skills saw, "
                    "not from an exact identity. Check each before relying on it.",
        "time-only": "Nothing was known about these objects beyond when and where "
                     "they were seen, so this is a plausible sequence, not an "
                     "identification.",
        "none": "No plausible next camera was found in the window.",
    }[method]
    return Journey(anchor=anchor, hops=hops, method=method, caveat=caveat)
