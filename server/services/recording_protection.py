# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Protect (or release) recorded footage from retention.

Flagged clips are skipped by retention's age and disk-pressure sweeps while
``protect_flagged`` is on. Shared by ``PUT /recordings/flag`` and
``POST /events/{id}/protect`` (HA-107).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session


def set_range_protection(
    db: Session, camera_id: int, start: datetime, end: datetime, flagged: bool
) -> int:
    """Flag every clip of *camera_id* that OVERLAPS ``[start, end)``; return
    how many rows changed. Commits.

    Overlap, not "starts inside": a 60 s segment that began before an
    incident holds the incident's first seconds, and selecting only clips
    that START in the range left exactly that clip unprotected. A clip with
    no ``end_time`` yet (still being written) counts when it began before
    ``end``.
    """
    from models import Recording

    updated = (
        db.query(Recording)
        .filter(
            Recording.camera_id == camera_id,
            Recording.start_time < end,
            or_(Recording.end_time.is_(None), Recording.end_time > start),
        )
        .update({Recording.is_flagged: flagged}, synchronize_session=False)
    )
    db.commit()
    return int(updated or 0)
