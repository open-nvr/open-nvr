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
ONVIF helper service: discovery, connection, media and PTZ operations.

This module wraps onvif-zeep sync clients in asyncio-friendly helpers via run_in_executor.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from core.logging_config import main_logger


# Lazy imports for optional deps
def _import_onvif():
    try:
        from onvif import ONVIFCamera  # type: ignore
        from onvif.client import ONVIFService  # type: ignore

        return ONVIFCamera, ONVIFService
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ONVIF library import failed: {e}")


def _import_ws_discovery():
    """Try multiple WS-Discovery implementations and return a unified adapter.

    Returns a tuple (Factory, QName, Scope) where Factory() -> instance with start(), stop(), searchServices(timeout).
    """
    # Try wsdiscovery (newer API with ThreadedWSDiscovery)
    try:
        from wsdiscovery import (
            QName,  # type: ignore
            Scope,  # type: ignore
        )
        from wsdiscovery.discovery import (
            ThreadedWSDiscovery as _ThreadedWSDiscovery,  # type: ignore
        )

        class _Factory:
            def __call__(self):
                return _ThreadedWSDiscovery()

        return _Factory(), QName, Scope
    except Exception:
        pass

    # Try WSDiscovery (older API)
    try:
        from WSDiscovery import (
            QName,  # type: ignore
            WSDiscovery as _WSDiscovery,  # type: ignore
        )

        class _Adapter:
            def __init__(self):
                self._impl = _WSDiscovery()

            def start(self):
                return self._impl.start()

            def stop(self):
                return self._impl.stop()

            def searchServices(self, timeout: int = 4):
                return self._impl.searchServices(timeout=timeout)

        class _Factory:
            def __call__(self):
                return _Adapter()

        class _Scope:  # placeholder, not used in our code path
            pass

        return _Factory(), QName, _Scope
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"WS-Discovery import failed: {e}. Install 'wsdiscovery' or 'WSDiscovery'.",
        )


# Default ONVIF port
DEFAULT_ONVIF_PORT = 80


async def _to_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def discover_onvif_devices(timeout: int = 4) -> list[dict[str, Any]]:
    """Discover ONVIF devices on local network using WS-Discovery.

    Returns list of dicts: {ip, xaddrs: [urls]}
    """
    _WSD, _QName, _Scope = _import_ws_discovery()

    def _discover_sync() -> list[dict[str, Any]]:
        wsd = _WSD()
        try:
            wsd.start()
            # Type filter for ONVIF devices is typically "dn:NetworkVideoTransmitter" but not all devices set it.
            # We'll broadcast without type filter and parse XAddrs.
            services = wsd.searchServices(timeout=timeout)
            results: list[dict[str, Any]] = []
            for svc in services:
                xaddrs = (
                    list(svc.getXAddrs()) if getattr(svc, "getXAddrs", None) else []
                )
                # Try to extract IP from XAddr, fallback to None
                ip = None
                for url in xaddrs:
                    # Example: http://192.168.1.50/onvif/device_service
                    try:
                        # Avoid dependency on urllib due to threading, keep simple parse
                        if "://" in url:
                            hostpart = url.split("://", 1)[1].split("/", 1)[0]
                            if ":" in hostpart:
                                ip = hostpart.split(":", 1)[0]
                            else:
                                ip = hostpart
                            break
                    except Exception as e:
                        main_logger.debug(f"XAddr parse error: {e}")
                        continue
                results.append(
                    {
                        "ip": ip,
                        "service_urls": xaddrs,
                    }
                )
            return results
        finally:
            try:
                wsd.stop()
            except Exception:
                pass

    try:
        return await _to_thread(_discover_sync)
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"WS-Discovery failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _make_camera(
    ip: str, username: str | None, password: str | None, port: int = DEFAULT_ONVIF_PORT
):
    ONVIFCamera, _ = _import_onvif()
    # onvif-zeep: wsdl dir is handled internally; pass None for autodetect
    return ONVIFCamera(ip, port, username or "", password or "")


async def get_media_service(
    ip: str, username: str, password: str, port: int = DEFAULT_ONVIF_PORT
):
    cam = await _to_thread(_make_camera, ip, username, password, port)
    return await _to_thread(cam.create_media_service)


async def get_device_service(
    ip: str, username: str, password: str, port: int = DEFAULT_ONVIF_PORT
):
    cam = await _to_thread(_make_camera, ip, username, password, port)
    return await _to_thread(cam.create_devicemgmt_service)


async def get_ptz_service(
    ip: str, username: str, password: str, port: int = DEFAULT_ONVIF_PORT
):
    cam = await _to_thread(_make_camera, ip, username, password, port)
    return await _to_thread(cam.create_ptz_service)


async def fetch_profiles(
    ip: str, username: str, password: str, port: int = DEFAULT_ONVIF_PORT
) -> list[dict[str, Any]]:
    try:
        media = await get_media_service(ip, username, password, port)
        profiles = await _to_thread(media.GetProfiles)
        out = []
        for p in profiles:
            token = getattr(p, "token", None)
            name = getattr(p, "Name", None)
            out.append({"token": token, "name": name})
        return out
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"GetProfiles failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"GetProfiles failed: {e}")


async def get_stream_uri(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    port: int = DEFAULT_ONVIF_PORT,
) -> str:
    try:
        media = await get_media_service(ip, username, password, port)
        req = await _to_thread(media.create_type, "GetStreamUri")
        # Transport: RTSP / UDP default; many NVRs use TCP, but we return whatever camera provides.
        req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
        req.ProfileToken = profile_token
        resp = await _to_thread(media.GetStreamUri, req)
        uri = getattr(resp, "Uri", None)
        if not uri:
            raise HTTPException(
                status_code=500, detail="Camera did not return a stream URI"
            )
        return uri
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"GetStreamUri failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"GetStreamUri failed: {e}")


async def ptz_continuous_move(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    x: float,
    y: float,
    z: float,
    port: int = DEFAULT_ONVIF_PORT,
) -> dict[str, Any]:
    try:
        ptz = await get_ptz_service(ip, username, password, port)
        req = await _to_thread(ptz.create_type, "ContinuousMove")
        req.ProfileToken = profile_token
        req.Velocity = {"PanTilt": {"x": x, "y": y}, "Zoom": {"x": z}}
        await _to_thread(ptz.ContinuousMove, req)
        return {"status": "moving"}
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"PTZ ContinuousMove failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PTZ move failed: {e}")


async def ptz_stop(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    port: int = DEFAULT_ONVIF_PORT,
) -> dict[str, Any]:
    try:
        ptz = await get_ptz_service(ip, username, password, port)
        req = await _to_thread(ptz.create_type, "Stop")
        req.ProfileToken = profile_token
        req.PanTilt = True
        req.Zoom = True
        await _to_thread(ptz.Stop, req)
        return {"status": "stopped"}
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"PTZ Stop failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PTZ stop failed: {e}")


async def ptz_presets(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    action: str,
    name: str | None = None,
    preset_token: str | None = None,
    port: int = DEFAULT_ONVIF_PORT,
) -> dict[str, Any]:
    try:
        ptz = await get_ptz_service(ip, username, password, port)
        action_lower = action.lower()
        if action_lower == "getpresets":
            req = await _to_thread(ptz.create_type, "GetPresets")
            req.ProfileToken = profile_token
            resp = await _to_thread(ptz.GetPresets, req)
            presets = []
            for p in resp or []:
                presets.append(
                    {
                        "token": getattr(p, "token", None),
                        "name": getattr(p, "Name", None),
                    }
                )
            return {"presets": presets}
        elif action_lower == "setpreset":
            req = await _to_thread(ptz.create_type, "SetPreset")
            req.ProfileToken = profile_token
            if name:
                req.PresetName = name
            if preset_token:
                req.PresetToken = preset_token
            resp = await _to_thread(ptz.SetPreset, req)
            # Some cameras return the new PresetToken
            return {
                "status": "ok",
                "preset_token": getattr(resp, "PresetToken", preset_token),
            }
        elif action_lower == "gotopreset":
            if not preset_token:
                raise HTTPException(
                    status_code=400, detail="preset_token is required for gotoPreset"
                )
            req = await _to_thread(ptz.create_type, "GotoPreset")
            req.ProfileToken = profile_token
            req.PresetToken = preset_token
            await _to_thread(ptz.GotoPreset, req)
            return {"status": "moving"}
        else:
            raise HTTPException(
                status_code=400,
                detail="Unsupported action. Use setPreset, getPresets, gotoPreset",
            )
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"PTZ Preset action failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PTZ preset failed: {e}")
