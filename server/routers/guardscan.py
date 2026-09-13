# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Entry-screening compliance: the list, the report, the export.

The read side of ``services/guardscan_event_consumer.py``. Every
screening the guard-scan app ruled on is here, the clean ones included,
because that is what makes a compliance figure mean anything: complete
scans over ALL screenings, not a count of complaints.

Three surfaces, one table:

* ``GET /guardscan/screenings`` — the searchable history, with the
  photos of who was scanned.
* ``GET /guardscan/report``     — day, week or month buckets, per guard
  and per camera, in the operator's local time.
* ``GET /guardscan/export``     — the same rows as CSV.

Scope: an operator sees screenings on cameras they may view
(``services.camera_scope``), exactly like alerts and events.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db, release
from core.permissions import RequirePermission
from core.pagination import resolve_total
from models import GuardScreening, User

logger = logging.getLogger(__name__)

#: Screenings are security records with photographs of customers, so
#: they sit behind the same permission as the alert inbox rather than a
#: new one — a new permission would be granted to nobody on an existing
#: install, and the page would be invisible until an admin noticed.
#: The SPA gates the page on the same name (see usePermissions), so the
#: two cannot drift into "the page is hidden but the API answers".
VIEW_PERMISSION = "alerts.view"

router = APIRouter(prefix="/guardscan", tags=["guard-scan"],
                   dependencies=[Depends(RequirePermission(VIEW_PERMISSION))])

VERDICTS = ("compliant", "partial", "incomplete", "no_scan")
_MAX_LIMIT = 200


def _scope(q, scope: set[int] | None):
    """Restrict to the caller's visible cameras.

    A screening with no resolvable camera is visible to nobody but a
    superuser: it cannot be scoped, and guessing in the permissive
    direction is how a camera someone may not see leaks.
    """
    if scope is None:
        return q
    if not scope:
        return q.filter(False)
    return q.filter(GuardScreening.camera_id.in_(sorted(scope)))


def _row_out(row: GuardScreening) -> dict:
    def _load(text):
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    return {
        "id": row.id,
        "session_id": row.session_id,
        "camera_id": row.camera_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "verdict": row.verdict,
        "score": row.score,
        "coverage": row.coverage,
        "order_score": row.order_score,
        "steps_done": _load(row.steps_done) or [],
        "steps_missing": _load(row.steps_missing) or [],
        "flagged": bool(row.flagged),
        "ended_by": row.ended_by,
        "duration_s": row.duration_s,
        "engaged_s": row.engaged_s,
        "guard_key": row.guard_key,
        "guard_name": row.guard_name,
        # Names only; the bytes come from the image route below.
        "images": sorted((_load(row.images) or {}).keys()),
    }


def _parse_bound(value: str | None):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@router.get("/screenings")
async def list_screenings(
    camera_id: int | None = Query(None),
    verdict: str | None = Query(None, description="compliant|partial|incomplete|no_scan"),
    guard: str | None = Query(None, description="Guard name or key"),
    flagged: bool | None = Query(None, description="Only scanner-flagged"),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    skip: int = Query(0, ge=0, le=100_000),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Screenings, newest first — the searchable history."""
    from services.camera_scope import visible_camera_ids

    q = _scope(db.query(GuardScreening), visible_camera_ids(db, current_user))
    if camera_id is not None:
        q = q.filter(GuardScreening.camera_id == camera_id)
    if verdict:
        q = q.filter(GuardScreening.verdict == verdict)
    if guard:
        like = f"%{guard}%"
        q = q.filter(GuardScreening.guard_name.ilike(like)
                     | GuardScreening.guard_key.ilike(like))
    if flagged is not None:
        q = q.filter(GuardScreening.flagged.is_(flagged))
    if (lower := _parse_bound(from_)) is not None:
        q = q.filter(GuardScreening.ended_at >= lower)
    if (upper := _parse_bound(to)) is not None:
        q = q.filter(GuardScreening.ended_at <= upper)

    rows = (q.order_by(GuardScreening.ended_at.desc(), GuardScreening.id.desc())
            .offset(skip).limit(limit).all())
    return {"screenings": [_row_out(r) for r in rows],
            "total": resolve_total(len(rows), skip, limit, q.count)}


@router.get("/report")
async def compliance_report(
    days: int = Query(7, ge=1, le=92),
    period: str = Query("day", description="day|week|month"),
    camera_id: int | None = Query(None),
    tz_offset_minutes: int = Query(0),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """How consistently the procedure was followed, by period and guard.

    Buckets are formed in the operator's LOCAL time, as the browser
    reports it: a shift that runs to 01:00 belongs to the day the guard
    thinks it does, not to whatever UTC says.
    """
    from services.camera_scope import visible_camera_ids

    now = datetime.now(UTC)
    local = timezone(timedelta(minutes=max(-14 * 60,
                                           min(14 * 60, int(tz_offset_minutes)))))
    start = now - timedelta(days=days)

    q = _scope(db.query(GuardScreening), visible_camera_ids(db, current_user))
    q = q.filter(GuardScreening.ended_at >= start)
    if camera_id is not None:
        q = q.filter(GuardScreening.camera_id == camera_id)
    rows = q.all()

    buckets: dict[str, dict] = {}
    guards: dict[str, dict] = {}
    cameras: dict[int, dict] = {}
    totals = _tally()

    for row in rows:
        when = row.ended_at
        if when is None:
            continue
        key, label = _bucket(when.astimezone(local), period)
        _count(buckets.setdefault(key, _tally(label=label, key=key)), row)
        who = row.guard_name or row.guard_key or "unidentified"
        _count(guards.setdefault(who, _tally(label=who)), row)
        cam = row.camera_id
        if cam is not None:
            _count(cameras.setdefault(cam, _tally(label=f"cam{cam}",
                                                  camera_id=cam)), row)
        _count(totals, row)

    return {
        "days": days,
        "period": period,
        "tz_offset_minutes": int(tz_offset_minutes),
        "generated_at": now.isoformat(),
        "totals": _finish(totals),
        "buckets": [_finish(b) for b in
                    sorted(buckets.values(), key=lambda b: b["key"], reverse=True)],
        "guards": [_finish(g) for g in
                   sorted(guards.values(), key=lambda g: -g["screenings"])],
        "cameras": [_finish(c) for c in
                    sorted(cameras.values(), key=lambda c: -c["screenings"])],
    }


def _tally(**extra) -> dict:
    out = {"screenings": 0, "flagged": 0, "score_sum": 0.0}
    out.update({v: 0 for v in VERDICTS})
    out.update(extra)
    return out


def _count(tally: dict, row: GuardScreening) -> None:
    tally["screenings"] += 1
    if row.verdict in tally:
        tally[row.verdict] += 1
    if row.flagged:
        tally["flagged"] += 1
    tally["score_sum"] += float(row.score or 0.0)


def _finish(tally: dict) -> dict:
    total = tally["screenings"]
    out = dict(tally)
    out.pop("score_sum", None)
    # Compliance is complete scans over ALL screenings. With nothing
    # screened it is not "100%" — it is not a number at all, and saying
    # 100 would put a green tile over a camera nobody walked past.
    out["compliance"] = round(100.0 * tally["compliant"] / total, 1) if total else None
    out["mean_score"] = round(tally["score_sum"] / total, 1) if total else None
    return out


def _bucket(when: datetime, period: str) -> tuple[str, str]:
    if period == "month":
        return when.strftime("%Y-%m"), when.strftime("%B %Y")
    if period == "week":
        year, week, _ = when.isocalendar()
        return f"{year:04d}-W{week:02d}", f"Week {week}, {year}"
    return when.strftime("%Y-%m-%d"), when.strftime("%a %d %b %Y")


@router.get("/export")
async def export_screenings(
    days: int = Query(30, ge=1, le=92),
    camera_id: int | None = Query(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """The screenings as CSV, for an audit file or a spreadsheet."""
    from services.camera_scope import visible_camera_ids

    q = _scope(db.query(GuardScreening), visible_camera_ids(db, current_user))
    q = q.filter(GuardScreening.ended_at >= datetime.now(UTC) - timedelta(days=days))
    if camera_id is not None:
        q = q.filter(GuardScreening.camera_id == camera_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ended_at", "camera_id", "guard", "verdict", "score",
                     "steps_done", "steps_missing", "flagged", "duration_s",
                     "ended_by", "session_id"])
    for row in q.order_by(GuardScreening.ended_at.desc()).limit(20_000):
        writer.writerow([
            row.ended_at.isoformat() if row.ended_at else "",
            row.camera_id or "",
            row.guard_name or row.guard_key or "",
            row.verdict,
            row.score,
            " ".join(json.loads(row.steps_done or "[]")),
            " ".join(json.loads(row.steps_missing or "[]")),
            "yes" if row.flagged else "no",
            row.duration_s or "",
            row.ended_by or "",
            row.session_id,
        ])
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="screenings-{stamp}.csv"'})


@router.get("/screenings/{screening_id}/images/{name}")
async def get_screening_image(
    screening_id: int,
    name: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """One evidence photo from a screening — who was scanned, and by whom."""
    from services.camera_scope import visible_camera_ids

    row = (_scope(db.query(GuardScreening), visible_camera_ids(db, current_user))
           .filter(GuardScreening.id == screening_id).first())
    if row is None or not row.images:
        raise HTTPException(status_code=404, detail="no such screening image")
    try:
        rel = (json.loads(row.images) or {}).get(name)
    except ValueError:
        rel = None
    if not isinstance(rel, str) or not rel:
        raise HTTPException(status_code=404, detail="no such screening image")

    from services.evidence_store import resolve_evidence

    path = resolve_evidence(rel)
    if path is None:
        raise HTTPException(status_code=404, detail="evidence file missing")
    release(db)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})
