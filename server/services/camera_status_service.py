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

"""
Camera connectivity tracker — turns MediaMTX path ready/not-ready signals into
debounced online/offline transitions.

Signals arrive from two directions:

* **Push** — MediaMTX ``runOnReady``/``runOnNotReady`` hooks hitting
  routers.mediamtx_hooks, which call ``handle_signal()``. Instant, but a hook
  curl can be lost if the backend is briefly down.
* **Pull** — ``reconcile_loop()`` polls ``/v3/paths/list`` as the safety net
  for missed hooks and for restarts of either process (MediaMTX dies without
  firing runOnNotReady; this backend restarts with empty in-memory state).

Committed transitions are published on the in-process event bus
(EVENT_CAMERA_STATUS → the /events/ws WebSocket) and persisted best-effort as
CameraEvent rows (event_type="connection", event_state active=offline /
inactive=restored), mirroring camera_event_manager.

State is in-memory only, like the event bus itself: after a restart the first
reconcile pass re-seeds silently so a reboot never produces an alert storm.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from core.logging_config import main_logger

# A source that drops and re-attaches within this window never surfaces as a
# transition — flapping cameras must not spam alerts.
OFFLINE_DEBOUNCE_SECONDS = 10.0

RECONCILE_INTERVAL_SECONDS = 30.0

# Delay before the first reconcile pass, so startup provisioning has settled
# and sources have had a chance to connect before we seed baseline state.
RECONCILE_INITIAL_DELAY_SECONDS = 30.0


class CameraStatusService:
    def __init__(self) -> None:
        # camera_id -> online? Cameras absent from the map are unknown: their
        # first signal seeds state silently instead of raising an alert.
        self._status: dict[int, bool] = {}
        self._pending_offline: dict[int, asyncio.Task] = {}
        self._seeded = False

    def is_online(self, camera_id: int) -> bool | None:
        return self._status.get(camera_id)

    async def handle_signal(
        self,
        *,
        camera_id: int,
        camera_name: str,
        path: str,
        online: bool,
        reason: str,
    ) -> None:
        """Ingest a ready/not-ready signal for a camera's main path."""
        if online:
            pending = self._pending_offline.pop(camera_id, None)
            if pending is not None:
                pending.cancel()
            previous = self._status.get(camera_id)
            self._status[camera_id] = True
            if previous is False:
                await self._emit(
                    camera_id=camera_id,
                    camera_name=camera_name,
                    path=path,
                    online=True,
                    reason=reason,
                )
            return

        if self._status.get(camera_id) is not True:
            # Already offline, or never seen online (e.g. still connecting at
            # boot) — record state, don't alert.
            self._status[camera_id] = False
            return
        if camera_id in self._pending_offline:
            return

        async def _commit() -> None:
            try:
                await asyncio.sleep(OFFLINE_DEBOUNCE_SECONDS)
                self._status[camera_id] = False
                await self._emit(
                    camera_id=camera_id,
                    camera_name=camera_name,
                    path=path,
                    online=False,
                    reason=reason,
                )
            except asyncio.CancelledError:
                pass
            finally:
                self._pending_offline.pop(camera_id, None)

        self._pending_offline[camera_id] = asyncio.create_task(_commit())

    async def _emit(
        self,
        *,
        camera_id: int,
        camera_name: str,
        path: str,
        online: bool,
        reason: str,
    ) -> None:
        from services.event_bus_service import publish_camera_status

        occurred_at = datetime.now(UTC)
        try:
            await publish_camera_status(
                camera_id=camera_id,
                status="online" if online else "offline",
                payload={
                    "path": path,
                    "camera_name": camera_name,
                    "reason": reason,
                    "occurred_at": occurred_at.isoformat(),
                },
            )
        except Exception:
            main_logger.exception("Failed to publish camera_status event")

        # Persist off the event loop: a blocking DB commit here would stall
        # every other async task (the pattern this overhaul removed elsewhere).
        await asyncio.to_thread(self._persist, camera_id, online, occurred_at)
        main_logger.info(
            "Camera %s (%s) is %s [%s]",
            camera_id, camera_name, "online" if online else "offline", reason,
        )

    def _persist(self, camera_id: int, online: bool, occurred_at: datetime) -> None:
        from core.database import SessionLocal
        from models import CameraEvent

        db = SessionLocal()
        try:
            db.add(CameraEvent(
                camera_id=camera_id,
                event_type="connection",
                event_state="inactive" if online else "active",
                description=(
                    "Camera stream restored" if online else "Camera stream offline"
                ),
                occurred_at=occurred_at,
            ))
            db.commit()
        except Exception:
            db.rollback()
            main_logger.exception("Failed to persist camera connection event")
        finally:
            db.close()

    async def reconcile_loop(self) -> None:
        await asyncio.sleep(RECONCILE_INITIAL_DELAY_SECONDS)
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                main_logger.exception("Camera status reconcile pass failed")
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)

    async def reconcile_once(self) -> None:
        from core.config import settings
        from core.database import SessionLocal
        from models import Camera
        from services.mediamtx_admin_service import MediaMtxAdminService
        from services.stream_service import _build_stream_name

        result = await MediaMtxAdminService.list_active_paths()
        if result.get("status") != "ok":
            # MediaMTX down or restarting. Its view of the world is gone, not
            # the cameras — skipping avoids mass-flapping the fleet offline.
            main_logger.debug(
                "Skipping camera status reconcile: MediaMTX paths/list -> %s",
                result.get("status"),
            )
            return

        ready_by_path: dict[str, bool] = {}
        for item in result.get("details", {}).get("items", []):
            name = item.get("name")
            if not name:
                continue
            ready_by_path[name] = bool(item.get("ready")) and (
                int(item.get("bytesReceived") or 0) > 0
            )

        db = SessionLocal()
        try:
            cameras: list[Any] = (
                db.query(Camera).filter(Camera.is_active == True).all()  # noqa: E712
            )
            cam_info = [
                (
                    c.id,
                    c.name or f"Camera {c.id}",
                    _build_stream_name(
                        settings.mediamtx_stream_prefix, c.id, c.ip_address
                    ),
                )
                for c in cameras
            ]
        finally:
            db.close()

        seeding = not self._seeded
        for camera_id, camera_name, path in cam_info:
            online = ready_by_path.get(path, False)
            if seeding:
                self._status[camera_id] = online
                continue
            if camera_id in self._pending_offline:
                # A hook-driven offline is already debouncing; the pending task
                # owns this camera's transition.
                continue
            if self._status.get(camera_id) == online:
                continue
            await self.handle_signal(
                camera_id=camera_id,
                camera_name=camera_name,
                path=path,
                online=online,
                reason="reconciler",
            )
        self._seeded = True


_service: CameraStatusService | None = None


def get_camera_status_service() -> CameraStatusService:
    global _service
    if _service is None:
        _service = CameraStatusService()
    return _service
