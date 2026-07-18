# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Hikvision vendor package — native control via ISAPI over HTTP Digest.

Covers the areas ONVIF can't reach well on Hikvision: OSD, motion, alert-stream
events, camera-user CRUD, SD-card storage, and richer network info. Everything
else falls back to the ONVIF baseline (the driver subclasses OnvifDriver).
"""

from __future__ import annotations

from .._probe import fingerprint_get
from .driver import HikvisionIsapiDriver

DRIVER = HikvisionIsapiDriver
# Below the OEM-family drivers so a device that both string-matches a rebrand and
# actually speaks ISAPI is confirmed here; well above the ONVIF fallback (1000).
PRIORITY = 10

# ISAPI signature path — present on every Hikvision-family device, 404 on Dahua.
_SIG_PATH = "/ISAPI/System/deviceInfo"


def matches(manufacturer: str) -> bool:
    return "hikvision" in manufacturer


async def probe(
    ip: str, port: int, username: str | None, password: str | None
) -> bool:
    """True iff the device serves the Hikvision ISAPI namespace.

    Accepts a 200 carrying a ``<DeviceInfo`` body (valid creds) or a 401 Digest
    challenge (endpoint exists, creds rejected — still Hikvision-family). A
    Dahua/ONVIF-only device 404s this path.
    """
    status, text = await fingerprint_get(ip, port, _SIG_PATH, username, password)
    if status == 200 and "<DeviceInfo" in text:
        return True
    return status == 401


__all__ = ["DRIVER", "PRIORITY", "HikvisionIsapiDriver", "matches", "probe"]
