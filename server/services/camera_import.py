"""Running a camera import: the rows services/camera_io.py planned, applied.

Each camera goes through the Cameras page's own code (create_camera_checked
and update_camera in routers/cameras.py), so an imported camera is checked,
resolved, provisioned and audited exactly like one added by hand. A few at a
time, in the background, each in its own database session: a thousand
cameras must neither hold the request open nor take the connection pool.

Jobs live in memory, in this one core process, for two hours: long enough
to watch one finish and download its results, and nothing to clean up.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from services.camera_io import PlannedCamera, summary

logger = logging.getLogger(__name__)

#: Cameras created at once. Each may dial the camera (ONVIF, a probe) and
#: provisions a MediaMTX path; a few in parallel keeps a big import moving
#: without hammering MediaMTX or the database pool.
CONCURRENCY = 3
#: How long a finished job's results stay readable.
KEEP_S = 2 * 3600


@dataclass
class ImportJob:
    id: str
    owner_id: int
    filename: str
    mode: str
    total: int
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    done: int = 0
    state: str = "running"
    results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        return {
            "id": self.id, "filename": self.filename, "mode": self.mode, "state": self.state,
            "total": self.total, "done": self.done, "counts": counts,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "results": sorted(self.results, key=lambda r: (r["row"], r.get("channel") or 0)),
        }


_JOBS: dict[str, ImportJob] = {}
# Strong references to the running tasks: asyncio keeps only weak ones.
_TASKS: set[asyncio.Task] = set()


def _prune() -> None:
    now = time.time()
    for jid, job in list(_JOBS.items()):
        if job.finished_at and now - job.finished_at > KEEP_S:
            _JOBS.pop(jid, None)


def get_job(job_id: str, owner_id: int) -> ImportJob | None:
    job = _JOBS.get(job_id)
    return job if job and job.owner_id == owner_id else None


def running_job(owner_id: int) -> ImportJob | None:
    return next((j for j in _JOBS.values() if j.owner_id == owner_id and j.state == "running"), None)


def start_import(owner_id: int, filename: str, planned: list[PlannedCamera], *, mode: str, probe: bool) -> ImportJob:
    """Start applying ``planned`` in the background and return the job."""
    _prune()
    job = ImportJob(id=uuid.uuid4().hex, owner_id=owner_id, filename=filename, mode=mode, total=len(planned))
    _JOBS[job.id] = job
    task = asyncio.get_running_loop().create_task(_run(job, planned, probe))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return job


async def _run(job: ImportJob, planned: list[PlannedCamera], probe: bool) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(p: PlannedCamera) -> None:
        async with sem:
            try:
                result = await _apply(p, job.owner_id, job.mode, probe)
            except Exception:  # pragma: no cover - _apply reports its own failures
                logger.exception("camera import: row %s failed unexpectedly", p.row)
                result = _result(p, "failed", "Unexpected error; see the server log.")
            job.results.append(result)
            job.done += 1

    try:
        await asyncio.gather(*(one(p) for p in planned))
    finally:
        job.finished_at = time.time()
        # Audit before saying "done": whoever is polling may act on it at once.
        _audit(job)
        job.state = "done"


def _result(p: PlannedCamera, outcome: str, message: str = "", camera_id: int | None = None) -> dict[str, Any]:
    return {
        "row": p.row, "channel": p.channel, "name": p.fields.get("name"),
        "outcome": outcome, "message": message, "camera_id": camera_id or p.camera_id,
    }


async def _apply(p: PlannedCamera, owner_id: int, mode: str, probe: bool) -> dict[str, Any]:
    if p.status in ("error", "duplicate"):
        return _result(p, "skipped", "; ".join(p.errors))
    if p.status == "unchanged":
        return _result(p, "unchanged")
    if p.status == "update" and mode != "add_update":
        return _result(p, "skipped", "Already on the system (updates were not asked for).")

    from core.database import SessionLocal
    from models import Camera, User
    from routers.cameras import create_camera_checked, update_camera
    from schemas import CameraCreate, CameraUpdate
    from services.camera_scope import can_manage_camera

    db = SessionLocal()
    try:
        user = db.get(User, owner_id)
        if user is None or not user.is_active:
            return _result(p, "failed", "The importing account is no longer active.")
        f = p.fields
        if p.status == "new":
            create = CameraCreate(
                name=f["name"], description=f.get("description"), ip_address=f["ip_address"],
                port=f.get("port", 554), username=f.get("username"), password=f.get("password"),
                rtsp_url=f.get("rtsp_url"), substream_url=f.get("substream_url"),
                location=f.get("location"), vlan=f.get("vlan"),
            )
            made = await create_camera_checked(
                create, force=True, db=db, current_user=user, request=None,
                probe=probe, duplicate_check=False,
            )
            if f.get("is_active") is False:
                cam = db.get(Camera, made.id)
                await update_camera(camera_id=made.id, camera_update=CameraUpdate(is_active=False),
                                    camera=cam, db=db, current_user=user, request=None)
            return _result(p, "created", camera_id=made.id)

        cam = db.get(Camera, p.camera_id)
        if cam is None or cam.deleted_at is not None:
            return _result(p, "failed", "The camera was deleted since the preview.")
        if not can_manage_camera(db, user, cam.id):
            return _result(p, "failed", "You may not change this camera.")
        changes = {name: f[name] for name in p.changes}
        await update_camera(camera_id=cam.id, camera_update=CameraUpdate(**changes),
                            camera=cam, db=db, current_user=user, request=None)
        return _result(p, "updated", "Changed: " + ", ".join(p.changes))
    except HTTPException as exc:
        db.rollback()
        detail = exc.detail
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        return _result(p, "failed", str(detail))
    except ValidationError as exc:
        db.rollback()
        return _result(p, "failed", "; ".join(e.get("msg", "") for e in exc.errors()))
    except Exception as exc:
        db.rollback()
        logger.exception("camera import: row %s (%s) failed", p.row, p.fields.get("name"))
        return _result(p, "failed", f"{type(exc).__name__}: {exc}"[:300])
    finally:
        db.close()


def _audit(job: ImportJob) -> None:
    from core.database import SessionLocal
    from services.audit_service import write_audit_log

    db = SessionLocal()
    try:
        write_audit_log(
            db, action="camera.import", user_id=job.owner_id, entity_type="camera",
            details={"file": job.filename, "mode": job.mode, "total": job.total,
                     "counts": job.as_dict()["counts"]},
        )
    except Exception:
        logger.exception("camera import: could not write the audit entry")
    finally:
        db.close()


__all__ = ["ImportJob", "get_job", "running_job", "start_import", "summary"]
