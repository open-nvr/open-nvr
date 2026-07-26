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
"""

from typing import Any

from fastapi import HTTPException

from core.logging_config import camera_logger
from services.onvif_digest_service import (
    fetch_profiles_digest,
    ptz_continuous_move_digest,
    ptz_stop_digest,
    resolve_control_endpoint,
)

# Simple in-memory cache: camera_id -> {scheme: str, port: int, profile_token: str}
_PTZ_CACHE: dict[int, dict[str, Any]] = {}


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
        _PTZ_CACHE.pop(camera_id, None)

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
        """Execute continuous move with caching."""
        config = PTZService._get_cached_config(camera_id)

        # If no config, find it
        if not config:
            camera_logger.info(f"PTZ: Cache miss for camera {camera_id}, scanning...")
            config = await PTZService._find_working_config(
                ip, username, password, camera_port
            )
            PTZService._update_cache(camera_id, config)

        try:
            # Attempt move
            await ptz_continuous_move_digest(
                ip, username, password, config["profile_token"], x, y, z,
                config["port"], config.get("scheme", "http")
            )
            return {
                "success": True,
                "camera_id": camera_id,
                "port": config["port"],
                "profile": config["profile_token"],
            }
        except Exception as e:
            # If it fails, maybe credentials or port changed?
            camera_logger.warning(
                f"PTZ: Cached config failed for camera {camera_id}: {e}. Retrying scan."
            )
            PTZService._invalidate_cache(camera_id)

            # Retry once
            try:
                config = await PTZService._find_working_config(
                    ip, username, password, camera_port
                )
                PTZService._update_cache(camera_id, config)

                await ptz_continuous_move_digest(
                    ip,
                    username,
                    password,
                    config["profile_token"],
                    x,
                    y,
                    z,
                    config["port"],
                    config.get("scheme", "http"),
                )
                return {
                    "success": True,
                    "camera_id": camera_id,
                    "port": config["port"],
                    "profile": config["profile_token"],
                }
            except Exception as final_e:
                camera_logger.error(
                    f"PTZ: Retry failed for camera {camera_id}: {final_e}"
                )
                raise HTTPException(status_code=500, detail=f"PTZ failed: {final_e}")

    @staticmethod
    async def stop(
        camera_id: int, ip: str, username: str, password: str, camera_port: int
    ) -> dict[str, Any]:
        """Execute stop with caching."""
        config = PTZService._get_cached_config(camera_id)

        if not config:
            config = await PTZService._find_working_config(
                ip, username, password, camera_port
            )
            PTZService._update_cache(camera_id, config)

        try:
            await ptz_stop_digest(
                ip, username, password, config["profile_token"], config["port"],
                config.get("scheme", "http")
            )
            return {"success": True, "camera_id": camera_id}
        except Exception:
            PTZService._invalidate_cache(camera_id)
            try:
                config = await PTZService._find_working_config(
                    ip, username, password, camera_port
                )
                PTZService._update_cache(camera_id, config)
                await ptz_stop_digest(
                    ip, username, password, config["profile_token"], config["port"],
                    config.get("scheme", "http")
                )
                return {"success": True, "camera_id": camera_id}
            except Exception as final_e:
                raise HTTPException(
                    status_code=500, detail=f"PTZ stop failed: {final_e}"
                )
