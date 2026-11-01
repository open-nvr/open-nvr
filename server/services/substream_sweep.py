# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Once per startup: find the substream of every camera that has none.

Discovery at camera creation (services.substream_discovery) covers cameras
added from now on. This covers the rest — cameras added before it existed,
cameras added while they were offline, imports — so an install converges
on "Tier-0 detects on the cheap stream" without anyone revisiting each
camera. It runs after MediaMTX provisioning, in the background, a few
cameras at a time, and dials only hosts inside the trust zone (the same
guard camera creation applies). A camera whose sub is found gets it stored
and its ``-sub`` path provisioned at once; a camera with nothing found is
left exactly as it was and tried again on the next startup — one short
probe, no state to keep.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

logger = logging.getLogger("opennvr.substream_sweep")

DEFAULT_CONCURRENCY = 3


def _host_of(url: str | None) -> str | None:
    try:
        return urlsplit(url or "").hostname
    except ValueError:
        return None


async def sweep_cameras(
    cameras: list,
    *,
    discover: Callable[..., Awaitable],
    store: Callable[[object, object], Awaitable[None]],
    host_ok: Callable[[str | None], bool],
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, int]:
    """Try every camera in ``cameras`` that has a main URL and no substream.

    ``discover(main_url, ip, username, password)`` is the discovery
    (substream_discovery.discover_substream); ``store(camera, found)`` is
    called for each hit. Pure apart from those two, so it tests without a
    database. Returns counts: ``tried``, ``found``, ``skipped``.
    """
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    counts = {"tried": 0, "found": 0, "skipped": 0}

    async def one(cam) -> None:
        main_url = getattr(cam, "rtsp_url", None)
        if not main_url or getattr(cam, "substream_url", None):
            counts["skipped"] += 1
            return
        if not host_ok(_host_of(main_url)):
            counts["skipped"] += 1
            return
        async with sem:
            counts["tried"] += 1
            try:
                found = await discover(
                    main_url, getattr(cam, "ip_address", None),
                    getattr(cam, "username", None), getattr(cam, "password", None),
                )
            except Exception:
                logger.debug("substream sweep: discovery failed for camera %s",
                             getattr(cam, "id", "?"), exc_info=True)
                return
            if found is None:
                return
            try:
                await store(cam, found)
                counts["found"] += 1
            except Exception:
                logger.warning("substream sweep: could not store the substream for "
                               "camera %s", getattr(cam, "id", "?"), exc_info=True)

    await asyncio.gather(*(one(c) for c in cameras))
    return counts


async def run_startup_sweep() -> dict[str, int] | None:
    """Load the active cameras, run the sweep, store and provision each hit.
    Never raises; None when it could not run at all."""
    from core.config import _host_is_internal, settings
    from core.database import SessionLocal
    from models import Camera, CameraConfig
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services.mediamtx_startup_service import MediaMtxStartupService
    from services.substream_discovery import discover_substream

    db = SessionLocal()
    try:
        cameras = (
            db.query(Camera)
            .filter(Camera.is_active == True)  # noqa: E712 - SQLAlchemy expression
            .filter(Camera.deleted_at.is_(None))
            .filter(Camera.substream_url.is_(None))
            .all()
        )
        if not cameras:
            return {"tried": 0, "found": 0, "skipped": 0}

        async def store(cam, found) -> None:
            cam.substream_url = found.url
            db.commit()
            logger.info(
                "camera %s: substream discovered via %s (%s) — Tier-0 will detect on it",
                cam.id, found.source, found.resolution,
            )
            if not (settings.mediamtx_admin_api and settings.mediamtx_auto_provision):
                return
            cfg = db.query(CameraConfig).filter(CameraConfig.camera_id == cam.id).first()
            await MediaMtxAdminService.upsert_path(
                cam.id, cam.ip_address,
                MediaMtxStartupService.build_provision_config(cam, cfg, db),
                transport_security=cfg.transport_security if cfg else None,
            )

        counts = await sweep_cameras(
            cameras, discover=discover_substream, store=store,
            host_ok=_host_is_internal,
        )
        logger.info("substream sweep: %d camera(s) without a substream, %d tried, "
                    "%d found", len(cameras), counts["tried"], counts["found"])
        return counts
    except Exception:
        logger.warning("substream sweep failed", exc_info=True)
        return None
    finally:
        db.close()
