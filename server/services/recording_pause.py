# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Pause and resume recording on one camera (HA-108).

OpenNVR is a recorder: recording is always on. A site admin can opt in to
letting it be paused (``site_settings.recording_pause_enabled``, off by
default), for example so Home Assistant can stop indoor cameras recording
while someone is home.

A pause:

* turns ``record`` off on the camera's MediaMTX path and stores
  ``CameraConfig.recording_enabled = False``, which the startup
  re-provisioner honours, so a restart does not silently resume it;
* is listed in site setting :data:`PAUSED_KEY` with who paused it, why, and
  an optional automatic resume time. Auto-resume is rescheduled at boot
  (:func:`restore_on_startup`), so a restart can't turn "pause for an hour"
  into "pause forever".

Turning the site flag off resumes every camera paused here
(:func:`resume_all`): with the flag off the rule is "always recording" again.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from core.logging_config import main_logger
from services import site_settings

#: camera id (str) -> {"since", "resume_at" (iso | None), "by", "reason"}
PAUSED_KEY = "recording_paused"

#: Automatic resume delay bounds (1 minute .. 7 days).
MIN_RESUME_AFTER_S = 60
MAX_RESUME_AFTER_S = 7 * 24 * 3600

_resume_tasks: dict[int, asyncio.Task] = {}


class PauseError(Exception):
    """MediaMTX refused the change; nothing was recorded as paused/resumed."""


def paused(db: Session) -> dict[str, dict[str, Any]]:
    value = site_settings.get_json(db, PAUSED_KEY, {})
    return value if isinstance(value, dict) else {}


def pause_info(db: Session, camera_id: int) -> dict[str, Any] | None:
    return paused(db).get(str(camera_id))


def set_recording_config(db: Session, camera_id: int, enable: bool) -> None:
    """Store the recording intent on the camera's CameraConfig (created if
    missing), which provisioning reads at startup. Commits."""
    from sqlalchemy.sql import func

    from core.config import settings
    from models import Camera, CameraConfig

    config = db.query(CameraConfig).filter(CameraConfig.camera_id == camera_id).first()
    if config:
        config.recording_enabled = enable
    else:
        cam = db.query(Camera).filter(Camera.id == camera_id).first()
        if cam and cam.rtsp_url:
            db.add(CameraConfig(
                camera_id=camera_id,
                stream_protocol="rtsp",
                source_url=cam.rtsp_url,
                recording_enabled=enable,
                rtsp_transport="tcp",
                recording_segment_seconds=settings.recording_segment_seconds,
                last_provisioned_at=func.now(),
            ))
    db.commit()


def _ok(result: dict[str, Any]) -> bool:
    return result.get("status") in ("ok", "success") or (
        result.get("status") != "error" and "recording_enabled" in result
    )


async def _apply(camera_id: int, enable: bool) -> None:
    from core.config import settings
    from services.mediamtx_admin_service import MediaMtxAdminService

    if enable:
        result = await MediaMtxAdminService.enable_recording(
            camera_id, duration=f"{settings.recording_segment_seconds}s", part_duration="1s")
    else:
        result = await MediaMtxAdminService.disable_recording(camera_id)
    if not _ok(result or {}):
        raise PauseError(str((result or {}).get("detail") or (result or {}).get("message")
                             or (result or {}).get("status") or "media server refused"))


def _cancel_resume(camera_id: int) -> None:
    task = _resume_tasks.pop(camera_id, None)
    if task is not None and not task.done():
        task.cancel()


def _schedule_resume(camera_id: int, resume_at: datetime) -> None:
    from core.background_tasks import spawn_background

    _cancel_resume(camera_id)

    async def _later() -> None:
        delay = (resume_at - datetime.now(UTC)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        from core.database import SessionLocal

        with SessionLocal() as db:
            info = pause_info(db, camera_id)
            # Resumed or re-paused (with a different time) meanwhile: not ours.
            if info is None or info.get("resume_at") != resume_at.isoformat():
                return
            # Unregister first: resume() cancels the registered task, and
            # that is this one.
            if _resume_tasks.get(camera_id) is asyncio.current_task():
                _resume_tasks.pop(camera_id, None)
            try:
                await resume(db, camera_id, actor="system:auto-resume", reason="scheduled resume")
            except Exception:  # noqa: BLE001 - logged; the pause stays listed
                main_logger.error("auto-resume of camera %s failed", camera_id, exc_info=True)

    _resume_tasks[camera_id] = spawn_background(_later(), name=f"recording-resume-{camera_id}")


async def pause(
    db: Session, camera_id: int, *, actor: str, reason: str | None = None,
    resume_after_s: int | None = None,
) -> dict[str, Any]:
    """Stop recording on *camera_id*. The caller has checked the site flag,
    the permission and the camera.

    Everything that can fail on bad input is worked out BEFORE recording is
    touched: a pause must never stop recording without also being listed
    (and so resumable by the timer, by turning the flag off, or in the UI).
    """
    if resume_after_s is not None and not (
            MIN_RESUME_AFTER_S <= resume_after_s <= MAX_RESUME_AFTER_S):
        raise ValueError(f"resume_after_s must be {MIN_RESUME_AFTER_S}..{MAX_RESUME_AFTER_S}")
    now = datetime.now(UTC)
    resume_at = now + timedelta(seconds=resume_after_s) if resume_after_s else None
    await _apply(camera_id, False)
    set_recording_config(db, camera_id, False)
    state = paused(db)
    state[str(camera_id)] = {
        "since": now.isoformat(),
        "resume_at": resume_at.isoformat() if resume_at else None,
        "by": actor,
        "reason": reason,
    }
    site_settings.set_json(db, PAUSED_KEY, state)
    if resume_at is not None:
        _schedule_resume(camera_id, resume_at)
    else:
        _cancel_resume(camera_id)
    return state[str(camera_id)]


async def resume(
    db: Session, camera_id: int, *, actor: str, reason: str | None = None,
) -> None:
    """Start recording on *camera_id* again (whether or not it was paused
    here: resuming is always allowed, the flag only guards pausing).

    A resume made by the system (``actor`` "system:...": the timer, boot,
    the flag turned off at boot) is audited here; one made through a route
    is audited by that route."""
    await _apply(camera_id, True)
    set_recording_config(db, camera_id, True)
    state = paused(db)
    if state.pop(str(camera_id), None) is not None:
        site_settings.set_json(db, PAUSED_KEY, state)
    _cancel_resume(camera_id)
    main_logger.info("recording resumed on camera %s by %s (%s)", camera_id, actor, reason)
    if actor.startswith("system:"):
        from services.audit_service import write_audit_log

        try:
            write_audit_log(db, action="recording.resume", entity_type="camera",
                            entity_id=camera_id, details={"actor": actor, "reason": reason})
        except Exception:  # noqa: BLE001 - the resume itself succeeded
            main_logger.error("could not audit the resume of camera %s", camera_id,
                              exc_info=True)


async def resume_all(db: Session, *, actor: str, reason: str) -> list[int]:
    """Resume every camera paused here; returns the ids resumed. A camera
    whose resume fails stays listed and is logged."""
    done = []
    for key in list(paused(db)):
        try:
            await resume(db, int(key), actor=actor, reason=reason)
            done.append(int(key))
        except Exception:  # noqa: BLE001
            main_logger.error("could not resume recording on camera %s", key, exc_info=True)
    return done


async def restore_on_startup() -> None:
    """Reschedule auto-resumes after a restart; resume the overdue ones.

    If the site flag was turned off while the process was down (e.g. by a
    DB edit), everything paused is resumed: the flag is the rule.
    """
    from core.database import SessionLocal

    with SessionLocal() as db:
        state = paused(db)
        if not state:
            return
        if not site_settings.recording_pause_enabled(db):
            await resume_all(db, actor="system:startup", reason="recording pause not allowed")
            return
        now = datetime.now(UTC)
        for key, info in state.items():
            at = info.get("resume_at") if isinstance(info, dict) else None
            if not at:
                continue
            try:
                resume_at = datetime.fromisoformat(at)
            except ValueError:
                continue
            if resume_at <= now:
                try:
                    await resume(db, int(key), actor="system:auto-resume",
                                 reason="resume time passed while stopped")
                except Exception:  # noqa: BLE001
                    main_logger.error("overdue resume of camera %s failed", key, exc_info=True)
            else:
                _schedule_resume(int(key), resume_at)
