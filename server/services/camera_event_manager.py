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
Camera event manager — long-lived per-camera subscriptions to the device's
native alarm stream (motion/tamper/etc).

Modeled on services.inference_manager: one asyncio task per subscribed camera,
started/stopped on demand and torn down in the app lifespan. Each event is
persisted (CameraEvent) and fanned out on the in-process event bus for the live
dashboard. Subscriptions are in-memory/opt-in (not auto-restarted after a
process restart — the same reconciler gap the AI inference loops have).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from core.logging_config import main_logger


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class CameraEventManager:
    def __init__(self) -> None:
        self.running_tasks: dict[int, asyncio.Task] = {}

    def is_running(self, camera_id: int) -> bool:
        t = self.running_tasks.get(camera_id)
        return t is not None and not t.done()

    async def start(self, camera_id: int) -> None:
        if self.is_running(camera_id):
            return
        self.running_tasks[camera_id] = asyncio.create_task(self._run(camera_id))

    async def stop(self, camera_id: int) -> None:
        t = self.running_tasks.pop(camera_id, None)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def stop_all(self) -> None:
        for cid in list(self.running_tasks):
            await self.stop(cid)

    async def _run(self, camera_id: int) -> None:
        from core.database import SessionLocal
        from services.camera_drivers.registry import get_driver_for_camera

        backoff = 3
        while True:
            try:
                db = SessionLocal()
                try:
                    driver = await get_driver_for_camera(db, camera_id)
                finally:
                    db.close()
                async for ev in driver.subscribe_events():
                    await self._handle(camera_id, ev)
                    backoff = 3  # healthy stream — reset backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:
                main_logger.warning(
                    "Camera %s event stream error: %s; retry in %ss",
                    camera_id, e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle(self, camera_id: int, ev: dict) -> None:
        from core.database import SessionLocal
        from models import CameraEvent
        from services.event_bus_service import publish_camera_event

        db = SessionLocal()
        try:
            row = CameraEvent(
                camera_id=camera_id,
                event_type=ev.get("event_type", "unknown"),
                event_state=ev.get("event_state"),
                description=ev.get("description"),
                occurred_at=_parse_dt(ev.get("occurred_at")),
            )
            db.add(row)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

        try:
            await publish_camera_event(
                camera_id=camera_id,
                event_type=ev.get("event_type", "unknown"),
                payload=ev,
            )
        except Exception:
            pass


_manager: CameraEventManager | None = None


def get_camera_event_manager() -> CameraEventManager:
    global _manager
    if _manager is None:
        _manager = CameraEventManager()
    return _manager
