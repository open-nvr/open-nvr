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
"""

from __future__ import annotations

from typing import Any, Iterable

from models import TimelineEvent, VisitDescriptor
from services import search_metrics as metrics


def apply_descriptors(
    db,
    row: TimelineEvent,
    descriptors: Iterable[Any],
    ran_tasks: Iterable[str] = (),
) -> int:
    """Write claims onto ``row``; returns how many were written.

    ``descriptors`` is any iterable of objects carrying ``kind``,
    ``value`` and optionally ``confidence`` / ``source_task`` /
    ``source_adapter`` / ``model_fingerprint`` — the endpoint passes its
    pydantic models, the enricher passes its own small dataclass. The
    caller commits.
    """
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
        existing.confidence = (
            None if d.confidence is None else max(0.0, min(1.0, float(d.confidence)))
        )
        existing.source_adapter = (d.source_adapter or "")[:60] or None
        existing.model_fingerprint = (d.model_fingerprint or "")[:120] or None
        written += 1
        # Attribution: which KAI-C skill is actually contributing claims.
        metrics.DESCRIPTORS_WRITTEN.inc({
            "kind": kind, "task": task or "unknown",
            "adapter": existing.source_adapter or "unknown",
        })

    if ran_tasks:
        # "Looked and found nothing" belongs on the row, not in a log:
        # the next reader has no other way to tell it from "never looked".
        seen = dict(row.payload or {})
        ran = sorted({*(seen.get("enriched_by") or []), *[str(t)[:40] for t in ran_tasks]})
        seen["enriched_by"] = ran
        row.payload = seen
    return written
