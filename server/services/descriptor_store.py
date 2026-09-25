# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Writing skill claims onto a visit — one implementation, two callers.

``POST /events/descriptors`` (an enricher outside core) and
``descriptor_enrichment`` (core's own, plan-driven one) must agree
exactly on what writing a claim means: the upsert key, what counts as a
conflict, and how "looked and found nothing" is recorded. Two copies of
these rules would drift, and the drift would be invisible — a claim
written one way and read another still looks like a claim.

The rules, all of which predate this module and are preserved verbatim:

* **Upsert per (visit, kind, task).** Re-running a skill replaces what
  IT said; a second task's disagreement is kept, because two views of
  one object is information about the skills, not a conflict to resolve
  in the schema.
* **A disagreeing value from another task is counted, not overwritten.**
  A skill quietly going wrong shows up no other way until somebody acts
  on its answer.
* **``ran_tasks`` lands on the row.** "Looked and found nothing" belongs
  next to the visit, not in a log: the next reader has no other way to
  tell it from "never looked", and treating the two alike makes a
  missing skill look like a mismatch.

And one rule this module adds: **a claim is also a word.** See
``project_attributes``.
"""

from __future__ import annotations

from typing import Any, Iterable

from models import EventText, TimelineEvent, VisitDescriptor
from services import search_metrics as metrics

#: Reading order for the projected words, so "red van" reads like a
#: description and two visits with the same claims produce the same
#: string. Anything not listed follows, alphabetically by kind.
_WORD_ORDER = ("colour", "clothing_top", "vehicle_type", "carrying", "plate")

#: Kinds whose value is an IDENTITY and is deliberately NOT projected
#: into free text. ``face_id`` is queried exactly, through the attr
#: filter, which already works — tokenising a person's name into the
#: text column would make any query containing that name match them, and
#: would widen who can discover the identity from the app that produced
#: it to anyone with search access. The claim is still stored, still
#: filterable, still an identity anchor for journeys; it simply is not a
#: search word.
_UNPROJECTED_KINDS = frozenset({"face_id"})


def project_attributes(db, row: TimelineEvent) -> str | None:
    """Recompute ``event_text.attributes`` from everything claimed about
    this visit. Returns the words written, or None if there are none.

    Why this exists: search matches free text against ``caption ||
    attributes`` (the expression migration ``c1d2e3f4a5b6`` indexes), and
    until now NOTHING wrote ``attributes``. So a visit could carry
    ``colour=red`` and ``vehicle_type=van`` as claims and still not
    answer "red van" — the structured claims and the searchable words
    never met, and an operator saw an empty result from a store that knew
    the answer.

    The claims stay the source of truth; this is a projection of them,
    recomputed from scratch every time rather than appended to, so a
    retracted or corrected claim cannot leave a stale word behind. The
    visit's ``plate_text`` joins them, because an operator searching a
    plate is doing exactly this kind of query and the read has already
    happened.

    ``caption`` is never touched. It belongs to the captioner, which has
    its own overwrite rules — and neither is ``source``, because
    ``caption_enrichment`` reads that field to decide whether somebody
    else has already described the visit. Stamping this module's name on
    it would make every projected visit look "already described" and
    silently stop it being captioned at all.

    The caller commits.
    """
    claims = (
        db.query(VisitDescriptor)
        .filter(VisitDescriptor.event_id == row.id)
        .all()
    )
    words: list[str] = []
    seen: set[str] = set()

    def _add(word: str | None) -> None:
        text = (word or "").strip().lower()
        if text and text not in seen:
            seen.add(text)
            words.append(text)

    def _rank(claim) -> tuple[int, str]:
        kind = (claim.kind or "").lower()
        return (_WORD_ORDER.index(kind) if kind in _WORD_ORDER
                else len(_WORD_ORDER), kind)

    for claim in sorted(claims, key=_rank):
        if (claim.kind or "").lower() in _UNPROJECTED_KINDS:
            continue
        _add(claim.value)
    # The plate lives on the row as a column and, since plate_enrichment
    # started writing it, as a claim too. Adding it here as well costs
    # nothing (the set dedupes) and keeps the words right on a visit read
    # before that change.
    _add(getattr(row, "plate_text", None))

    attributes = " ".join(words) or None
    existing = db.get(EventText, row.id)
    if existing is None:
        if attributes is None:
            # No words and no sidecar: leave the table sparse. Most
            # visits never get text, and that is the reason it is a
            # sidecar rather than columns on ``events``.
            return None
        db.add(EventText(event_id=row.id, attributes=attributes))
        return attributes
    existing.attributes = attributes
    if attributes is None and not (existing.caption or "").strip():
        # Every claim retracted and nobody described it: the row now says
        # nothing. Deleting it is what the ingest endpoint does in the
        # same situation, and an empty sidecar row would otherwise make
        # "enriched" and "enriched to nothing" look alike.
        db.delete(existing)
        return None
    return attributes


#: The ways a claim's SUBJECT can have been determined — which visit it
#: is about. See RFC-0003 and the migration that added the column.
#:
#:   direct   the producer held the event_id
#:   window   core matched camera + instant inside the visit's own span
#:   nearest  nothing covered the instant; the closest within a bounded
#:            tolerance was used — a guess, and named one
#:
#: There is deliberately no value for "we chose between two candidates":
#: an ambiguous instant binds nothing at all.
BINDINGS = frozenset({"direct", "window", "nearest"})


def apply_descriptors(
    db,
    row: TimelineEvent,
    descriptors: Iterable[Any],
    ran_tasks: Iterable[str] = (),
    *,
    binding: str = "direct",
) -> int:
    """Write claims onto ``row``; returns how many were written.

    ``descriptors`` is any iterable of objects carrying ``kind``,
    ``value`` and optionally ``confidence`` / ``source_task`` /
    ``source_adapter`` / ``model_fingerprint`` — the endpoint passes its
    pydantic models, the enricher passes its own small dataclass. The
    caller commits.

    ``binding`` says how the SUBJECT was determined and defaults to
    ``direct``, which is what every caller before RFC-0003 was: they
    held the ``event_id`` already. It is stored per claim rather than
    per event because one visit can carry a plate the LPR pipeline
    bound directly and a face an app bound by timestamp, and a reader
    that trusts one must be able to refuse the other.
    """
    binding = (binding or "direct").strip().lower()
    if binding not in BINDINGS:
        raise ValueError(
            f"binding must be one of {sorted(BINDINGS)}, not {binding!r}")
    written = 0
    for d in descriptors:
        kind = (d.kind or "").strip().lower()[:40]
        value = (d.value or "").strip().lower()[:120]
        if not kind or not value:
            continue
        task = (d.source_task or "")[:40] or None
        existing = (
            db.query(VisitDescriptor)
            .filter(
                VisitDescriptor.event_id == row.id,
                VisitDescriptor.kind == kind,
                VisitDescriptor.source_task.is_(task) if task is None
                else VisitDescriptor.source_task == task,
            )
            .one_or_none()
        )
        if existing is None:
            # Another TASK may already have claimed this kind. That is a
            # disagreement between skills, not a duplicate to overwrite,
            # and counting it is the only way a skill quietly going wrong
            # shows up before somebody acts on its answer.
            other = (
                db.query(VisitDescriptor)
                .filter(
                    VisitDescriptor.event_id == row.id,
                    VisitDescriptor.kind == kind,
                    VisitDescriptor.value != value,
                )
                .first()
            )
            if other is not None:
                metrics.DESCRIPTOR_CONFLICTS.inc({"kind": kind})
            existing = VisitDescriptor(event_id=row.id, kind=kind, source_task=task)
            db.add(existing)
        existing.value = value
        # Re-running a skill replaces how ITS claim was bound too: a
        # doorbell that first guessed by timestamp and later matched a
        # real visit must not leave the old "nearest" behind, or the
        # row would understate what is now known.
        existing.binding = binding
        existing.confidence = (
            None if d.confidence is None else max(0.0, min(1.0, float(d.confidence)))
        )
        existing.source_adapter = (d.source_adapter or "")[:60] or None
        existing.model_fingerprint = (d.model_fingerprint or "")[:120] or None
        # The audit join. Kept when a re-run does not carry one: a claim
        # that was evidence must not become an assertion because the
        # second writer forgot the id.
        cid = getattr(d, "correlation_id", None)
        if isinstance(cid, str) and cid.strip():
            existing.correlation_id = cid.strip()[:64]
        written += 1
        # Attribution: which KAI-C skill is actually contributing claims.
        metrics.DESCRIPTORS_WRITTEN.inc({
            "kind": kind, "task": task or "unknown",
            "adapter": existing.source_adapter or "unknown",
            # Watchable: a deployment where "nearest" is climbing is one
            # where apps are guessing more than they are measuring.
            "binding": binding,
        })

    if ran_tasks:
        # "Looked and found nothing" belongs on the row, not in a log:
        # the next reader has no other way to tell it from "never looked".
        seen = dict(row.payload or {})
        ran = sorted({*(seen.get("enriched_by") or []), *[str(t)[:40] for t in ran_tasks]})
        seen["enriched_by"] = ran
        row.payload = seen

    # A claim is also a word. Projecting here rather than at each call
    # site is the same argument that put the upsert here: the endpoint
    # and the enricher must not be able to disagree about whether a claim
    # is searchable, and a claim that is filterable but not findable is
    # the kind of half-wiring that makes an operator think the store is
    # empty when it is not.
    project_attributes(db, row)
    return written


#: The canonical task name a plate read is attributed to — the same
#: string ``enrichment_plan.TASK_DESCRIPTORS`` keys the skill under, and
#: the same one the plan advertises as producing ``kind="plate"``.
PLATE_TASK = "license_plate_recognition"


class _PlateClaim:
    """The shape ``apply_descriptors`` reads, for one plate read."""

    kind = "plate"
    source_task = PLATE_TASK
    model_fingerprint = None

    def __init__(self, value: str, confidence: float | None,
                 adapter: str | None, correlation_id: str | None = None) -> None:
        self.value = value
        self.confidence = confidence
        self.source_adapter = adapter
        self.correlation_id = correlation_id


def sync_plate_claim(db, row: TimelineEvent) -> None:
    """Make the row's plate a CLAIM as well as a column.

    ``plate_enrichment`` has always written ``plate_text`` onto the event
    row and stopped there, so the plate was the one thing the box could
    read perfectly and the only skill output that never became a
    descriptor. Two features were dark because of it. The attr filter
    could not answer ``plate:ab12cde`` even though the plan advertises
    ``license_plate_recognition -> kinds: ["plate"]``. And
    ``journey.py``, whose whole premise is that "a plate read by the LPR
    adapter ... landing here as a descriptor is the same string on two
    cameras", had nothing to anchor on: ``ANCHOR_KINDS`` names ``plate``
    first, and no plate claim had ever been written.

    This costs no inference. The read already happened; this is the same
    answer, recorded where the rest of the store looks for it.

    Called on BOTH edges — a plate landing and a plate being retracted —
    because a claim that outlives the read it came from is worse than no
    claim: ``clear_plate`` exists precisely for reads the later looks
    overturned, and a journey anchored on a retracted plate would put
    somebody at a camera they were never at.
    """
    plate = (getattr(row, "plate_text", None) or "").strip()
    payload = row.payload if isinstance(row.payload, dict) else {}
    if plate:
        confidence = payload.get("plate_confidence")
        apply_descriptors(db, row, [_PlateClaim(
            plate,
            # A measured read, unlike a VQA answer — so the number is
            # real and worth keeping. Its absence (a forwarded bus read
            # that carried none) stays None rather than becoming a
            # flattering default.
            float(confidence) if isinstance(confidence, (int, float))
            and not isinstance(confidence, bool) else None,
            str(payload.get("plate_source") or "") or None,
            str(payload.get("correlation_id") or "") or None,
        )])
        return

    removed = (
        db.query(VisitDescriptor)
        .filter(
            VisitDescriptor.event_id == row.id,
            VisitDescriptor.kind == "plate",
            VisitDescriptor.source_task == PLATE_TASK,
        )
        .delete(synchronize_session=False)
    )
    if removed:
        # Only reproject when something actually went: the projection is
        # a query per call and every plate write lands on this path.
        project_attributes(db, row)
