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
PTZ Service: Handles PTZ logic with port/profile caching to optimize performance.
Helps resolve N+1 issue where every move request probes multiple ports.

Every command to one camera runs under that camera's lock, in the order the
commands reached it. A press is a move and a release a stop; before the lock
the two raced through the same lookups, and on a quick tap the stop could
reach the camera first and leave it moving.
"""

import asyncio
import time
from typing import Any

from fastapi import HTTPException

from core.logging_config import camera_logger
from services.onvif_digest_service import (
    NO_PTZ_DETAIL,
    fetch_profiles_digest,
    forget_ptz_service_url,
    ptz_continuous_move_digest,
    ptz_get_presets_digest,
    ptz_goto_preset_digest,
    ptz_set_preset_digest,
    ptz_stop_digest,
    resolve_control_endpoint,
)

# Simple in-memory cache: camera_id -> {scheme: str, port: int, profile_token: str}
_PTZ_CACHE: dict[int, dict[str, Any]] = {}

#: One lock per camera; see the module docstring.
_LOCKS: dict[int, asyncio.Lock] = {}

#: When each camera's ONVIF endpoint was last re-resolved. A rescan walks
#: every candidate port and scheme at up to 4 s each, over a minute for a
#: camera that does not answer at all, so a failing camera gets one per
#: ``RESCAN_COOLDOWN_S`` and every other failure in between is reported
#: straight away.
_LAST_RESCAN: dict[int, float] = {}
RESCAN_COOLDOWN_S = 60.0

#: A scan that found no working endpoint, and when: repeated for the same
#: cooldown instead of scanning again, so a camera that is off the network
#: costs one scan a minute, not one per button press.
_RESOLVE_FAILED: dict[int, tuple[float, HTTPException]] = {}

#: What PTZ calls have shown about a camera: True when it took a PTZ
#: command, False when it advertised no PTZ service. Absent = never tried.
#: Read by the camera list to hide PTZ controls on cameras that have none.
_PTZ_SUPPORT: dict[int, bool] = {}

#: Failures a fresh endpoint scan cannot fix: no PTZ service, a refused
#: login, a bad preset token, a camera that timed out (one that does not
#: answer on its known port will not answer on the others either).
_NO_RESCAN_STATUS = {401, 409, 422, 504}


def ptz_support(camera_id: int) -> bool | None:
    """True / False when a PTZ call has settled it for this camera, else None."""
    return _PTZ_SUPPORT.get(camera_id)


def _lock(camera_id: int) -> asyncio.Lock:
    lock = _LOCKS.get(camera_id)
    if lock is None:
        lock = _LOCKS[camera_id] = asyncio.Lock()
    return lock


class PTZService:
    @staticmethod
    async def _find_working_config(
        ip: str, username: str, password: str, camera_port: int
    ) -> dict[str, Any]:
        """Resolve the working ONVIF control endpoint + a profile token.

        Uses the shared ``resolve_control_endpoint`` (single source of truth for
        ports AND scheme — http or https) instead of a private, narrower port
        list, so PTZ reaches cameras on any control port (e.g. 8088) exactly
        like the rest of the driver layer.
        """
        hint = camera_port if camera_port and camera_port != 554 else None
        scheme, port = await resolve_control_endpoint(ip, hint)
        try:
            profiles = await fetch_profiles_digest(
                ip, username, password, port, scheme
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to find ONVIF profile for {ip}. Last error: {e}",
            ) from e

        if not profiles:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to find ONVIF profile for {ip}: no media profiles",
            )

        # Prefer a profile with 'Main' in the name if available, else the first.
        token = profiles[0].get("token", "Profile_1")
        for p in profiles:
            if "main" in str(p.get("name", "")).lower():
                token = p.get("token")
                break

        camera_logger.info(
            f"PTZ: Found working config at {scheme}://{ip}:{port} token={token}"
        )
        return {"scheme": scheme, "port": port, "profile_token": token}

    @staticmethod
    def _get_cached_config(camera_id: int) -> dict[str, Any] | None:
        return _PTZ_CACHE.get(camera_id)

    @staticmethod
    def _update_cache(camera_id: int, config: dict[str, Any]):
        _PTZ_CACHE[camera_id] = config

    @staticmethod
    def _invalidate_cache(camera_id: int):
        config = _PTZ_CACHE.pop(camera_id, None)
        if config:
            # The PTZ XAddr was learned from that endpoint; drop it with it.
            forget_ptz_service_url(
                config.get("ip", ""), config["port"], config.get("scheme", "http")
            )

    @staticmethod
    async def _resolve(
        camera_id: int, ip: str, username: str, password: str, camera_port: int
    ) -> dict[str, Any]:
        failed = _RESOLVE_FAILED.get(camera_id)
        if failed is not None:
            at, error = failed
            if time.monotonic() - at < RESCAN_COOLDOWN_S:
                raise error
        _LAST_RESCAN[camera_id] = time.monotonic()
        try:
            config = await PTZService._find_working_config(
                ip, username, password, camera_port)
        except HTTPException as e:
            _RESOLVE_FAILED[camera_id] = (time.monotonic(), e)
            raise
        _RESOLVE_FAILED.pop(camera_id, None)
        config["ip"] = ip
        PTZService._update_cache(camera_id, config)
        return config

    @staticmethod
    def _record(camera_id: int, error: Exception | None) -> None:
        if error is None:
            _PTZ_SUPPORT[camera_id] = True
        elif isinstance(error, HTTPException) and error.detail == NO_PTZ_DETAIL:
            _PTZ_SUPPORT[camera_id] = False

    @staticmethod
    async def _run(
        camera_id: int, ip: str, username: str, password: str, camera_port: int, op,
    ):
        """Run ``op(config)`` against the cached ONVIF config, under the
        camera's lock, re-resolving the config once if the cached one fails
        in a way a rescan could fix (port or scheme changed) and none has
        run in the last ``RESCAN_COOLDOWN_S``."""
        async with _lock(camera_id):
            config = PTZService._get_cached_config(camera_id)
            if not config:
                config = await PTZService._resolve(
                    camera_id, ip, username, password, camera_port)
            try:
                result = await op(config)
                PTZService._record(camera_id, None)
                return result
            except HTTPException as e:
                PTZService._record(camera_id, e)
                if e.status_code in _NO_RESCAN_STATUS:
                    raise
                first_error: Exception = e
            except Exception as e:  # noqa: BLE001 - maybe retried below
                first_error = e
            since = time.monotonic() - _LAST_RESCAN.get(camera_id, float("-inf"))
            if since < RESCAN_COOLDOWN_S:
                camera_logger.warning(
                    f"PTZ: camera {camera_id} failed again ({first_error}); "
                    f"endpoint rescanned {since:.0f} s ago, not rescanning"
                )
                if isinstance(first_error, HTTPException):
                    raise first_error
                raise HTTPException(status_code=502, detail=f"PTZ failed: {first_error}")
            camera_logger.warning(
                f"PTZ: Cached config failed for camera {camera_id}: {first_error}. Retrying scan."
            )
            PTZService._invalidate_cache(camera_id)
            config = await PTZService._resolve(
                camera_id, ip, username, password, camera_port)
            try:
                result = await op(config)
            except Exception as e:
                PTZService._record(camera_id, e)
                raise
            PTZService._record(camera_id, None)
            return result

    @staticmethod
    async def move(
        camera_id: int,
        ip: str,
        username: str,
        password: str,
        camera_port: int,
        x: float,
        y: float,
        z: float,
    ) -> dict[str, Any]:
        """Continuous move: pan/tilt/zoom velocities in -1..1."""
        async def op(cfg):
            await ptz_continuous_move_digest(
                ip, username, password, cfg["profile_token"], x, y, z,
                cfg["port"], cfg.get("scheme", "http"))
            return {"success": True, "camera_id": camera_id,
                    "port": cfg["port"], "profile": cfg["profile_token"]}
        return await PTZService._run(camera_id, ip, username, password, camera_port, op)

    @staticmethod
    async def stop(
        camera_id: int, ip: str, username: str, password: str, camera_port: int
    ) -> dict[str, Any]:
        """Stop pan, tilt and zoom."""
        async def op(cfg):
            await ptz_stop_digest(
                ip, username, password, cfg["profile_token"], cfg["port"],
                cfg.get("scheme", "http"))
            return {"success": True, "camera_id": camera_id}
        return await PTZService._run(camera_id, ip, username, password, camera_port, op)

    @staticmethod
    async def presets(
        camera_id: int, ip: str, username: str, password: str, camera_port: int
    ) -> list[dict[str, str]]:
        async def op(cfg):
            return await ptz_get_presets_digest(
                ip, username, password, cfg["profile_token"], cfg["port"],
                cfg.get("scheme", "http"))
        return await PTZService._run(camera_id, ip, username, password, camera_port, op)

    @staticmethod
    async def goto_preset(
        camera_id: int, ip: str, username: str, password: str, camera_port: int,
        preset_token: str,
    ) -> dict[str, Any]:
        async def op(cfg):
            return await ptz_goto_preset_digest(
                ip, username, password, cfg["profile_token"], preset_token,
                cfg["port"], cfg.get("scheme", "http"))
        return await PTZService._run(camera_id, ip, username, password, camera_port, op)

    @staticmethod
    async def set_preset(
        camera_id: int, ip: str, username: str, password: str, camera_port: int,
        name: str, preset_token: str | None = None,
    ) -> dict[str, Any]:
        async def op(cfg):
            return await ptz_set_preset_digest(
                ip, username, password, cfg["profile_token"], name, preset_token,
                cfg["port"], cfg.get("scheme", "http"))
        return await PTZService._run(camera_id, ip, username, password, camera_port, op)
